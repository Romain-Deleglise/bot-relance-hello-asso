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
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from string import Template

from .config import Config
from .membership import Membership

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

    Les templates sont des fichiers séparés du code, éditables sans toucher au
    Python. Variables disponibles :
    `$nom`, `$prenom`, `$nom_complet`, `$email`, `$date_fin`, `$date_adhesion`,
    `$formule`, `$association`, `$lien_adhesion`.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.text_template = self._read(config.template_text, required=True)
        self.html_template = self._read(config.template_html, required=False)

    @staticmethod
    def _read(path: str, required: bool) -> str | None:
        if not path:
            return None
        file = Path(path)
        if not file.is_file():
            if required:
                raise MailError(f"Template introuvable : {file}")
            logger.info("Template HTML absent (%s), envoi en texte brut uniquement", file)
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

    def render(self, membership: Membership) -> RenderedMail:
        context = self.context(membership)
        # `safe_substitute` : un `$` oublié dans un template ne fait pas
        # échouer toute l'exécution.
        return RenderedMail(
            subject=Template(self.config.mail_subject).safe_substitute(context),
            text=Template(self.text_template or "").safe_substitute(context),
            html=(
                Template(self.html_template).safe_substitute(context)
                if self.html_template
                else None
            ),
        )


def build_message(
    config: Config, membership: Membership, mail: RenderedMail
) -> EmailMessage:
    """Assemble le message final (texte + HTML alternatif)."""
    message = EmailMessage()
    message["Subject"] = mail.subject
    message["From"] = formataddr((config.mail_from_name or None, config.mail_from))
    message["To"] = formataddr((membership.full_name or None, membership.email))
    if config.mail_reply_to:
        message["Reply-To"] = config.mail_reply_to
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

    def send(self, membership: Membership, mail: RenderedMail) -> None:
        message = self.build_message(membership, mail)
        try:
            self._connect().send_message(message)
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError):
            # Une déconnexion en cours de série ne doit pas perdre le reste.
            logger.warning("Connexion SMTP perdue, reconnexion")
            self._server = None
            try:
                self._connect().send_message(message)
            except (smtplib.SMTPException, OSError) as exc:
                raise MailError(f"Envoi à {membership.email} impossible : {exc}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"Envoi à {membership.email} impossible : {exc}") from exc

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

    def send(self, membership: Membership, mail: RenderedMail) -> None:
        logger.info(
            "[DRY-RUN] Mail non envoyé → %s <%s> | échéance %s | sujet : %s",
            membership.display_name,
            membership.email,
            membership.end_date,
            mail.subject,
        )
        if not self.dump_dir:
            return

        self._index += 1
        stem = f"{self._index:03d}-{self._safe_name(membership.email)}"
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
