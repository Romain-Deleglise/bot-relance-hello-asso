"""Rendu des templates et envoi des mails de relance.

Le canal d'envoi par défaut est **SMTP** (serveur de l'association ou relais
transactionnel type Brevo / Mailjet / Sendgrid, qui exposent tous un endpoint
SMTP). C'est le point d'insertion unique des identifiants d'envoi : ils sont
lus depuis la configuration (`SMTP_*`), jamais écrits dans le code.

Pour passer à une API HTTP transactionnelle plutôt qu'à SMTP, il suffit
d'écrire une classe exposant la même méthode `send(...)` et de l'injecter dans
`run()` (cf. `cli.py`).
"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from string import Template

from .config import Config
from .membership import (
    STAGE_EXPIRATION,
    STAGE_PREAVIS,
    Membership,
    Reminder,
)

logger = logging.getLogger(__name__)


class MailError(Exception):
    """Échec d'envoi d'un mail."""


def _format_fr_date(value) -> str:
    """Date au format jour/mois/année, lisible dans le corps du mail."""
    return value.strftime("%d/%m/%Y") if value else ""


@dataclass
class RenderedMail:
    subject: str
    text: str
    html: str | None


class MailRenderer:
    """Rendu des templates de mail (substitution `$variable`, cf. string.Template).

    Un jeu de templates par étape de relance : le préavis et le mail du jour
    d'expiration n'ont pas le même ton. Si les templates de l'étape
    « expiration » sont absents, ceux du préavis sont réutilisés, de sorte
    qu'un seul jeu de textes suffit à faire tourner le bot.

    Les templates sont des fichiers séparés du code, éditables sans toucher au
    Python. Variables disponibles :
    `$nom`, `$prenom`, `$nom_complet`, `$email`, `$date_fin`, `$date_adhesion`,
    `$formule`, `$association`, `$lien_adhesion`.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        preavis_text = self._read(config.template_text, required=True)
        preavis_html = self._read(config.template_html, required=False)
        self._templates: dict[str, tuple[str, str | None, str]] = {
            STAGE_PREAVIS: (preavis_text or "", preavis_html, config.mail_subject),
            STAGE_EXPIRATION: (
                self._read(config.template_text_expiration, required=False)
                or preavis_text
                or "",
                self._read(config.template_html_expiration, required=False)
                or preavis_html,
                config.mail_subject_expiration or config.mail_subject,
            ),
        }

    @staticmethod
    def _read(path: str, required: bool) -> str | None:
        if not path:
            return None
        file = Path(path)
        if not file.is_file():
            if required:
                raise MailError(f"Template introuvable : {file}")
            logger.info("Template absent (%s), repli sur le template générique", file)
            return None
        return file.read_text(encoding="utf-8")

    def context(self, membership: Membership) -> dict[str, str]:
        return {
            "nom": membership.last_name,
            "prenom": membership.first_name,
            "nom_complet": membership.display_name,
            "email": membership.email,
            "date_fin": _format_fr_date(membership.end_date),
            "date_adhesion": _format_fr_date(membership.order_date),
            "formule": membership.tier_name,
            "association": self.config.association_name,
            "lien_adhesion": self.config.renewal_url,
        }

    def render(self, reminder: Reminder) -> RenderedMail:
        context = self.context(reminder.membership)
        text, html, subject = self._templates[reminder.stage]
        # `safe_substitute` : un `$` oublié dans un template ne fait pas
        # échouer toute l'exécution.
        return RenderedMail(
            subject=Template(subject).safe_substitute(context),
            text=Template(text).safe_substitute(context),
            html=Template(html).safe_substitute(context) if html else None,
        )


def build_message(
    config: Config, membership: Membership, mail: RenderedMail
) -> EmailMessage:
    """Assemble le message final (texte + HTML alternatif)."""
    message = EmailMessage()
    message["Subject"] = mail.subject
    message["From"] = formataddr((config.mail_from_name or None, config.mail_from))
    message["To"] = formataddr((membership.full_name or None, membership.email))
    # `Date` et `Message-ID` explicites : leur absence pénalise la délivrabilité
    # (filtres anti-spam) et casse le fil de discussion côté client. On les pose
    # nous-mêmes plutôt que de compter sur le serveur d'envoi, et pour que les
    # `.eml` produits en dry-run soient des messages complets.
    message["Date"] = formatdate(localtime=True)
    domain = config.mail_from.rpartition("@")[2] or None
    message["Message-ID"] = make_msgid(domain=domain)
    if config.mail_reply_to:
        message["Reply-To"] = config.mail_reply_to
    # List-Unsubscribe (mailto) : améliore la délivrabilité et laisse un moyen
    # simple de se désinscrire. Pas de variante « One-Click » HTTP, qui exigerait
    # un endpoint web dédié.
    unsubscribe = config.unsubscribe_email or config.mail_reply_to or config.mail_from
    if unsubscribe:
        message["List-Unsubscribe"] = f"<mailto:{unsubscribe}?subject=Desabonnement>"
    if config.mail_bcc:
        message["Bcc"] = config.mail_bcc
    message.set_content(mail.text)
    if mail.html:
        message.add_alternative(mail.html, subtype="html")
    return message


class SmtpMailer:
    """Envoi SMTP, avec une connexion réutilisée pour toute l'exécution."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._server: smtplib.SMTP | smtplib.SMTP_SSL | None = None
        self._sent_count = 0

    def __enter__(self) -> "SmtpMailer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _connect(self) -> smtplib.SMTP | smtplib.SMTP_SSL:
        if self._server is not None:
            return self._server
        cfg = self.config
        try:
            if cfg.smtp_use_ssl:
                server: smtplib.SMTP = smtplib.SMTP_SSL(
                    cfg.smtp_host, cfg.smtp_port, timeout=30,
                    context=ssl.create_default_context(),
                )
            else:
                server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
                if cfg.smtp_use_tls:
                    server.starttls(context=ssl.create_default_context())
            if cfg.smtp_user:
                server.login(cfg.smtp_user, cfg.smtp_password)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"Connexion SMTP impossible ({cfg.smtp_host}:{cfg.smtp_port}) : {exc}") from exc
        self._server = server
        return server

    def build_message(self, membership: Membership, mail: RenderedMail) -> EmailMessage:
        return build_message(self.config, membership, mail)

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """Un rejet SMTP 4xx (greylisting, throttling) est réessayable."""
        code = getattr(exc, "smtp_code", None)
        if isinstance(code, int) and 400 <= code < 500:
            return True
        recipients = getattr(exc, "recipients", None)
        if recipients:
            codes = [c for c, _ in recipients.values()]
            return bool(codes) and all(400 <= c < 500 for c in codes)
        return False

    def _pace(self) -> None:
        """Temporise avant un envoi (sauf le premier) et reconnecte au besoin."""
        if self._sent_count == 0:
            return
        if self.config.smtp_delay_seconds > 0:
            time.sleep(self.config.smtp_delay_seconds)
        every = self.config.smtp_max_per_connection
        if every and self._sent_count % every == 0:
            logger.debug("Reconnexion SMTP après %s envois", self._sent_count)
            self.close()

    def send(self, reminder: Reminder, mail: RenderedMail) -> None:
        membership = reminder.membership
        message = self.build_message(membership, mail)

        self._pace()

        attempts = max(1, self.config.smtp_retry_attempts)
        delay = self.config.smtp_retry_delay_seconds
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                self._connect().send_message(message)
                self._sent_count += 1
                return
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
                # Déconnexion : on repart sur une connexion neuve.
                logger.warning("Connexion SMTP perdue, reconnexion (%s)", exc)
                self._server = None
                last_error = exc
            except (smtplib.SMTPException, OSError) as exc:
                if not self._is_transient(exc):
                    raise MailError(
                        f"Envoi à {membership.email} impossible : {exc}"
                    ) from exc
                # Rejet temporaire (4xx) : on repart au propre et on réessaie.
                self._server = None
                last_error = exc

            if attempt < attempts:
                logger.warning(
                    "Envoi à %s : rejet temporaire, nouvel essai %s/%s dans %.0f s (%s)",
                    membership.email, attempt + 1, attempts, delay, last_error,
                )
                time.sleep(delay)
                delay *= 2

        raise MailError(
            f"Envoi à {membership.email} impossible après {attempts} essais : {last_error}"
        )

    def close(self) -> None:
        if self._server is not None:
            try:
                self._server.quit()
            except smtplib.SMTPException:
                pass
            self._server = None


class DryRunMailer:
    """Mailer de simulation : n'envoie rien.

    Journalise ce qui serait envoyé et, si `dump_dir` est fourni, écrit sur
    disque le mail rendu pour chaque destinataire : un `.eml` (le message
    exact, ouvrable dans n'importe quel client mail), un `.txt` et, le cas
    échéant, un `.html`. C'est le moyen de relire les textes avant le premier
    envoi réel.
    """

    def __init__(self, config: Config, dump_dir: str | None = None) -> None:
        self.config = config
        self.dump_dir = Path(dump_dir) if dump_dir else None
        self._index = 0
        if self.dump_dir:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Les mails simulés seront écrits dans %s", self.dump_dir)

    def __enter__(self) -> "DryRunMailer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    @staticmethod
    def _safe_name(email: str) -> str:
        """Nom de fichier sûr dérivé de l'adresse."""
        return re.sub(r"[^A-Za-z0-9._@-]", "_", email)[:80]

    def send(self, reminder: Reminder, mail: RenderedMail) -> None:
        membership = reminder.membership
        logger.info(
            "[DRY-RUN] Mail non envoyé → %s <%s> | échéance %s | étape %s | sujet : %s",
            membership.display_name,
            membership.email,
            membership.end_date,
            reminder.stage_label,
            mail.subject,
        )
        if not self.dump_dir:
            return

        self._index += 1
        stem = (
            f"{self._index:03d}-{reminder.stage}-{self._safe_name(membership.email)}"
        )
        try:
            message = build_message(self.config, membership, mail)
            (self.dump_dir / f"{stem}.eml").write_bytes(message.as_bytes())
            (self.dump_dir / f"{stem}.txt").write_text(mail.text, encoding="utf-8")
            if mail.html:
                (self.dump_dir / f"{stem}.html").write_text(mail.html, encoding="utf-8")
        except OSError as exc:
            # Un disque plein ne doit pas faire échouer une simulation.
            logger.error("Écriture du mail simulé impossible (%s) : %s", stem, exc)

    def close(self) -> None:
        return None
