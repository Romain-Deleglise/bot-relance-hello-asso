"""Point d'entrée : orchestration d'une exécution de relance.

Enchaînement : configuration → authentification → récupération des adhésions →
calcul des échéances → filtrage → anti-doublon → envoi → journalisation.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import socket
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import notify, suppression
from .config import Config, ConfigError, load_env_file
from .helloasso import (
    VALIDITY_MOVING_YEAR,
    HelloAssoAuthError,
    HelloAssoClient,
    HelloAssoError,
)
from .mailer import (
    DryRunMailer,
    MailConnectionError,
    MailError,
    MailRenderer,
    SmtpMailer,
)
from .membership import (
    Membership,
    Reminder,
    dedupe_memberships,
    normalize_item,
    parse_date,
    select_to_remind,
    today_in,
)
from .state import ReminderStore

logger = logging.getLogger("relance_adhesions")


def setup_logging(level: str, log_file: str = "") -> None:
    """Journalisation console + fichier (rotation) pour le suivi du cron.

    Le dossier du fichier de log est créé au besoin. S'il reste inaccessible
    (droits, disque plein), on se rabat sur la sortie standard plutôt que de
    faire échouer toute l'exécution : perdre le log ne doit pas empêcher les
    relances de partir.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    file_error: str = ""
    if log_file:
        try:
            parent = Path(log_file).parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
                )
            )
        except OSError as exc:
            file_error = f"Journalisation fichier désactivée ({log_file}) : {exc}"

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        handlers=handlers,
        force=True,
    )
    if file_error:
        logger.warning(file_error)


class FormValidityResolver:
    """Résout, avec cache, la règle d'échéance de chaque formulaire d'adhésion.

    Un appel API par formulaire au plus, quel que soit le nombre d'adhésions.
    """

    def __init__(self, client: HelloAssoClient, organization_slug: str) -> None:
        self.client = client
        self.organization_slug = organization_slug
        self._cache: dict[tuple[str, str], tuple[str, date | None]] = {}

    def resolve(self, form_type: str, form_slug: str) -> tuple[str, date | None]:
        """Renvoie `(validity_type, form_end_date)`."""
        key = (form_type or "Membership", form_slug)
        if key in self._cache:
            return self._cache[key]

        validity: tuple[str, date | None] = (VALIDITY_MOVING_YEAR, None)
        if form_slug:
            try:
                form = self.client.get_form_public(self.organization_slug, *key)
                validity = (
                    form.get("validityType") or VALIDITY_MOVING_YEAR,
                    parse_date(form.get("endDate")),
                )
                logger.info(
                    "Formulaire %s : validityType=%s, endDate=%s",
                    form_slug, validity[0], validity[1],
                )
            except HelloAssoError as exc:
                logger.warning(
                    "Formulaire %s illisible (%s) : repli sur l'année glissante",
                    form_slug, exc,
                )
        self._cache[key] = validity
        return validity


def collect_memberships(
    client: HelloAssoClient, config: Config, today: date
) -> list[Membership]:
    """Récupère et normalise toutes les adhésions pertinentes."""
    resolver = FormValidityResolver(client, config.helloasso_organization_slug)

    # On ne remonte pas au-delà de `max_membership_age_days` : au premier
    # lancement, cela évite d'analyser (et potentiellement de relancer) tout
    # l'historique de l'association.
    date_from = (
        datetime.combine(today - timedelta(days=config.max_membership_age_days),
                         datetime.min.time(), tzinfo=timezone.utc)
        .isoformat()
    )

    memberships: list[Membership] = []
    analysed = 0
    for item in client.iter_membership_items(
        config.helloasso_organization_slug, date_from=date_from
    ):
        analysed += 1
        order: dict[str, Any] = item.get("order") or {}
        form_slug = (order.get("formSlug") or "").strip()
        if config.form_slugs and form_slug not in config.form_slugs:
            continue

        validity_type, form_end_date = resolver.resolve(
            order.get("formType") or "Membership", form_slug
        )
        membership = normalize_item(
            item, validity_type, form_end_date, config.membership_duration_days,
            config.timezone,
        )
        if membership is not None:
            memberships.append(membership)

    logger.info("%s items d'adhésion analysés, %s exploitables", analysed, len(memberships))
    return memberships


def run(config: Config, today: date | None = None, dump_dir: str | None = None) -> int:
    """Exécute une passe complète. Renvoie un code de sortie (0 = succès)."""
    today = today or today_in(config.timezone)
    sent = errors = 0

    hostname = socket.gethostname()

    def supervise(ok: bool, detail: str = "") -> None:
        """Supervision par canaux indépendants du mail (jamais en dry-run)."""
        if config.dry_run:
            return
        notify.ping_healthcheck(config.healthcheck_url, success=ok)
        if not ok:
            notify.send_alert(
                config.alert_webhook_url,
                f"[Relance adhésions {config.association_name}] "
                f"ÉCHEC sur {hostname} : {detail}",
            )

    if config.dry_run:
        logger.warning("MODE DRY-RUN : aucun mail ne sera réellement envoyé")
    else:
        config.validate_for_sending()
        if config.mail_redirect_to:
            logger.warning(
                "MODE TEST : tous les mails seront redirigés vers %s "
                "(les adhérents ne reçoivent rien, aucun envoi n'est enregistré)",
                config.mail_redirect_to,
            )

    renderer = MailRenderer(config)
    client = HelloAssoClient(
        client_id=config.helloasso_client_id,
        client_secret=config.helloasso_client_secret,
        api_base=config.helloasso_api_base,
        auth_base=config.helloasso_auth_base,
    )

    with ReminderStore(config.state_db) as store:
        run_id = store.start_run(config.dry_run)
        try:
            memberships = collect_memberships(client, config, today)
        except HelloAssoAuthError as exc:
            logger.error("Authentification HelloAsso impossible : %s", exc)
            store.finish_run(run_id, 0, 0, 0, 1)
            supervise(False, f"authentification HelloAsso impossible : {exc}")
            return 2
        except HelloAssoError as exc:
            logger.error("API HelloAsso indisponible : %s", exc)
            store.finish_run(run_id, 0, 0, 0, 1)
            supervise(False, f"API HelloAsso indisponible : {exc}")
            return 3

        # Liste d'exclusion (désinscription) : on retire ces adresses avant tout.
        suppressed = suppression.load_suppressed(config.suppression_file)
        if suppressed:
            avant = len(memberships)
            memberships = [
                m for m in memberships if m.email.lower() not in suppressed
            ]
            retires = avant - len(memberships)
            if retires:
                logger.info(
                    "%s adhésion(s) écartée(s) via la liste de désinscription", retires
                )

        analysed = len(memberships)
        latest = dedupe_memberships(memberships)
        selected = select_to_remind(
            latest, today, config.days_before_expiry, config.days_after_expiry
        )
        logger.info(
            "Préavis : échéance entre demain et %s | Expiration : échéance "
            "entre %s et aujourd'hui → %s relance(s) à envoyer",
            today + timedelta(days=config.days_before_expiry),
            today - timedelta(days=config.days_after_expiry),
            len(selected),
        )

        # Anti-doublon : on écarte ce qui a déjà été relancé pour cette échéance.
        pending = [r for r in selected if not store.already_sent(r.dedup_key)]
        skipped = len(selected) - len(pending)
        if skipped:
            logger.info("%s relance(s) déjà envoyée(s) précédemment, ignorée(s)", skipped)

        # Garde-fou : en cas de bug (mauvais calcul d'échéance), on refuse de
        # transformer une exécution en campagne d'e-mailing massive.
        if len(pending) > config.max_emails_per_run:
            logger.error(
                "%s relances à envoyer, au-delà de la limite MAX_EMAILS_PER_RUN=%s. "
                "Exécution interrompue par sécurité : vérifiez la configuration "
                "ou relancez en dry-run.",
                len(pending), config.max_emails_per_run,
            )
            store.finish_run(run_id, analysed, len(selected), 0, 1)
            supervise(
                False,
                f"garde-fou déclenché : {len(pending)} relances > "
                f"MAX_EMAILS_PER_RUN={config.max_emails_per_run}, rien envoyé",
            )
            return 4

        if pending:
            logger.info("Destinataires retenus :")
            for reminder in pending:
                membership = reminder.membership
                logger.info(
                    "  · %-32s %-34s échéance %s  [%s]  (%s, formulaire « %s »)",
                    membership.display_name,
                    membership.email,
                    membership.end_date,
                    reminder.stage_label,
                    membership.validity_type,
                    membership.form_slug or "?",
                )

        mailer = (
            DryRunMailer(config, dump_dir) if config.dry_run else SmtpMailer(config)
        )
        mail_down = False
        with mailer:
            for reminder in pending:
                membership = reminder.membership
                try:
                    mailer.send(reminder, renderer.render(reminder))
                except MailConnectionError as exc:
                    # Panne du canal d'envoi : inutile d'insister sur chaque
                    # destinataire. On arrête et on alertera (canal indépendant).
                    errors += 1
                    mail_down = True
                    logger.error(
                        "Service de mail injoignable, arrêt des envois : %s", exc
                    )
                    break
                except MailError as exc:
                    errors += 1
                    logger.error("Échec d'envoi : %s", exc)
                    continue
                sent += 1
                if not config.dry_run and not config.mail_redirect_to:
                    # En mode test (redirection), on n'enregistre RIEN : l'envoi
                    # réel ultérieur doit bien partir à tous les adhérents.
                    # On n'enregistre qu'après un envoi réellement réussi. Une
                    # erreur d'écriture (disque plein, verrou) ne doit pas
                    # interrompre le reste du batch ; elle est signalée, et au
                    # pire cet adhérent sera relancé au prochain passage — mieux
                    # qu'un arrêt total en plein envoi.
                    try:
                        store.mark_sent(
                            reminder.dedup_key,
                            membership.item_id,
                            membership.email,
                            membership.end_date,
                        )
                    except sqlite3.Error as exc:
                        errors += 1
                        logger.error(
                            "Mail envoyé à %s mais enregistrement anti-doublon "
                            "impossible (%s) : risque de relance au prochain passage",
                            membership.email, exc,
                        )
                logger.info(
                    "Relance %s (%s) → %s <%s> (échéance %s)",
                    "simulée" if config.dry_run else "envoyée",
                    reminder.stage_label,
                    membership.display_name, membership.email, membership.end_date,
                )

        store.finish_run(run_id, analysed, len(selected), sent, errors)

    logger.info(
        "Bilan : %s adhésion(s) analysée(s), %s dans la fenêtre, %s relance(s) %s, %s erreur(s)",
        analysed, len(selected), sent,
        "simulée(s)" if config.dry_run else "envoyée(s)", errors,
    )

    # Supervision finale : ping de succès si tout est propre, alerte sinon.
    if mail_down:
        supervise(
            False,
            f"service de mail injoignable — {sent} envoyé(s), "
            f"{len(pending) - sent} relance(s) non partie(s)",
        )
    elif errors:
        supervise(False, f"{errors} erreur(s) d'envoi sur {len(pending)} relance(s)")
    else:
        supervise(True)

    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relance-adhesions",
        description="Relance automatique des adhésions HelloAsso arrivant à échéance.",
    )
    parser.add_argument(
        "--env-file", default=".env",
        help="Fichier de configuration à charger (défaut : .env, ignoré s'il est absent)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simule l'exécution sans envoyer aucun mail",
    )
    parser.add_argument(
        "--days-before", type=int, default=None,
        help="Nombre de jours avant l'échéance déclenchant la relance",
    )
    parser.add_argument(
        "--days-after", type=int, default=None,
        help="Relance aussi les adhésions expirées depuis au plus N jours",
    )
    parser.add_argument(
        "--dump-dir", default=None,
        help="Dossier où écrire les mails rendus (.eml/.txt/.html) en dry-run, "
             "pour les relire avant tout envoi réel",
    )
    parser.add_argument(
        "--unsubscribe", metavar="EMAIL", default=None,
        help="Ajoute une adresse à la liste d'exclusion (désinscription) puis quitte. "
             "Cette adresse ne sera plus jamais relancée.",
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument(
        "--today", default=None,
        help="Date de référence AAAA-MM-JJ (tests / rattrapage)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.env_file:
            try:
                load_env_file(args.env_file)
            except ConfigError:
                # Absence de .env acceptable : les variables peuvent venir de
                # l'environnement (Docker, systemd, cron).
                pass
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        return 2

    # Les options de ligne de commande priment sur la configuration.
    if args.dry_run or args.dump_dir:
        # --dump-dir n'a de sens qu'en simulation : il l'active implicitement,
        # pour qu'une relecture des mails n'envoie jamais rien par mégarde.
        config.dry_run = True
    if args.days_before is not None:
        config.days_before_expiry = args.days_before
    if args.days_after is not None:
        config.days_after_expiry = args.days_after
    if args.log_level:
        config.log_level = args.log_level

    setup_logging(config.log_level, config.log_file)

    # Désinscription : ajoute l'adresse à la liste d'exclusion et s'arrête.
    if args.unsubscribe:
        try:
            ajoutee = suppression.add_suppressed(config.suppression_file, args.unsubscribe)
        except ValueError as exc:
            logger.error("Désinscription impossible : %s", exc)
            return 2
        if ajoutee:
            logger.info(
                "%s a été désinscrit : plus aucune relance ne lui sera envoyée.",
                args.unsubscribe.strip().lower(),
            )
        else:
            logger.info("%s est déjà désinscrit.", args.unsubscribe.strip().lower())
        return 0

    today = None
    if args.today:
        try:
            today = date.fromisoformat(args.today)
        except ValueError:
            logger.error("--today doit être au format AAAA-MM-JJ")
            return 2

    try:
        return run(config, today, args.dump_dir)
    except ConfigError as exc:
        logger.error("Configuration invalide : %s", exc)
        _alert_unexpected(config, f"configuration invalide : {exc}")
        return 2
    except MailError as exc:
        logger.error("Erreur d'envoi : %s", exc)
        _alert_unexpected(config, f"erreur d'envoi : {exc}")
        return 5
    except Exception as exc:  # noqa: BLE001 - garantit une trace dans le log du cron
        logger.exception("Erreur inattendue")
        _alert_unexpected(config, f"erreur inattendue : {exc}")
        return 1


def _alert_unexpected(config: Config, detail: str) -> None:
    """Alerte de dernier recours pour une erreur remontée jusqu'à main().

    Best effort et jamais en dry-run : on ne veut ni bruit de test, ni qu'un
    problème d'alerte masque l'erreur d'origine déjà journalisée.
    """
    if config.dry_run:
        return
    try:
        notify.ping_healthcheck(config.healthcheck_url, success=False)
        notify.send_alert(
            config.alert_webhook_url,
            f"[Relance adhésions {config.association_name}] "
            f"ÉCHEC sur {socket.gethostname()} : {detail}",
        )
    except Exception:  # noqa: BLE001 - l'alerte ne doit jamais masquer l'erreur
        logger.exception("Échec de l'alerte de supervision")


if __name__ == "__main__":
    sys.exit(main())
