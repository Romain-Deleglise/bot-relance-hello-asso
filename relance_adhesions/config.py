"""Chargement et validation de la configuration.

Tous les identifiants (API HelloAsso, SMTP) proviennent de variables
d'environnement, éventuellement alimentées par un fichier `.env` non versionné.
Rien n'est écrit en dur dans le code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """Configuration absente ou invalide."""


def load_env_file(path: str | os.PathLike[str]) -> None:
    """Charge un fichier `KEY=value` dans os.environ (sans écraser l'existant).

    Implémentation volontairement minimale pour éviter une dépendance
    supplémentaire (python-dotenv). Les lignes vides et les commentaires (`#`)
    sont ignorés, les guillemets entourant la valeur sont retirés.
    """
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"Fichier de configuration introuvable : {file}")

    for raw_line in file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(f"Variable d'environnement obligatoire manquante : {name}")
    return value or ""


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un entier (valeur reçue : {raw!r})") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "oui", "on"}


def _get_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default) or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class Config:
    """Configuration complète du bot de relance."""

    # --- API HelloAsso -----------------------------------------------------
    helloasso_client_id: str
    helloasso_client_secret: str
    helloasso_organization_slug: str
    helloasso_api_base: str = "https://api.helloasso.com/v5"
    helloasso_auth_base: str = "https://api.helloasso.com"

    # --- Fenêtres de relance (en jours) ------------------------------------
    # Une relance est envoyée quand la date de fin d'adhésion tombe dans
    # l'intervalle [aujourd'hui - days_after_expiry ; aujourd'hui + days_before_expiry].
    days_before_expiry: int = 15
    days_after_expiry: int = 0

    # Durée de validité par défaut (jours) pour les formulaires en année
    # glissante (`MovingYear` chez HelloAsso). 365 = 12 mois glissants.
    membership_duration_days: int = 365

    # Ne relance pas une adhésion payée il y a plus de N jours : garde-fou
    # contre un rattrapage massif au premier lancement.
    max_membership_age_days: int = 800

    # Restreint la recherche à certains formulaires (slugs). Vide = tous les
    # formulaires d'adhésion de l'organisation.
    form_slugs: list[str] = field(default_factory=list)

    # --- Envoi des mails ---------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True  # STARTTLS sur port 587
    smtp_use_ssl: bool = False  # SMTPS direct sur port 465
    mail_from: str = ""
    mail_from_name: str = ""
    mail_reply_to: str = ""
    mail_bcc: str = ""
    mail_subject: str = "Votre adhésion arrive à échéance"

    # --- Contenu ------------------------------------------------------------
    association_name: str = "Pause IA"
    renewal_url: str = ""
    template_text: str = "templates/relance.txt"
    template_html: str = "templates/relance.html"

    # --- Exécution ----------------------------------------------------------
    state_db: str = "data/relances.sqlite3"
    log_file: str = ""
    log_level: str = "INFO"
    dry_run: bool = False
    max_emails_per_run: int = 100  # garde-fou anti-spam en cas de bug

    @classmethod
    def from_env(cls) -> "Config":
        """Construit la configuration depuis os.environ."""
        return cls(
            helloasso_client_id=_get("HELLOASSO_CLIENT_ID", required=True),
            helloasso_client_secret=_get("HELLOASSO_CLIENT_SECRET", required=True),
            helloasso_organization_slug=_get("HELLOASSO_ORGANIZATION_SLUG", required=True),
            helloasso_api_base=_get("HELLOASSO_API_BASE", "https://api.helloasso.com/v5"),
            helloasso_auth_base=_get("HELLOASSO_AUTH_BASE", "https://api.helloasso.com"),
            days_before_expiry=_get_int("RELANCE_DAYS_BEFORE_EXPIRY", 15),
            days_after_expiry=_get_int("RELANCE_DAYS_AFTER_EXPIRY", 0),
            membership_duration_days=_get_int("RELANCE_MEMBERSHIP_DURATION_DAYS", 365),
            max_membership_age_days=_get_int("RELANCE_MAX_MEMBERSHIP_AGE_DAYS", 800),
            form_slugs=_get_list("RELANCE_FORM_SLUGS"),
            smtp_host=_get("SMTP_HOST"),
            smtp_port=_get_int("SMTP_PORT", 587),
            smtp_user=_get("SMTP_USER"),
            smtp_password=_get("SMTP_PASSWORD"),
            smtp_use_tls=_get_bool("SMTP_USE_TLS", True),
            smtp_use_ssl=_get_bool("SMTP_USE_SSL", False),
            mail_from=_get("MAIL_FROM"),
            mail_from_name=_get("MAIL_FROM_NAME", "Pause IA"),
            mail_reply_to=_get("MAIL_REPLY_TO"),
            mail_bcc=_get("MAIL_BCC"),
            mail_subject=_get("MAIL_SUBJECT", "Votre adhésion arrive à échéance"),
            association_name=_get("ASSOCIATION_NAME", "Pause IA"),
            renewal_url=_get("RENEWAL_URL"),
            template_text=_get("TEMPLATE_TEXT", "templates/relance.txt"),
            template_html=_get("TEMPLATE_HTML", "templates/relance.html"),
            state_db=_get("STATE_DB", "data/relances.sqlite3"),
            log_file=_get("LOG_FILE"),
            log_level=_get("LOG_LEVEL", "INFO"),
            dry_run=_get_bool("DRY_RUN", False),
            max_emails_per_run=_get_int("MAX_EMAILS_PER_RUN", 100),
        )

    def validate_for_sending(self) -> None:
        """Vérifie que tout est en place pour un envoi réel (hors dry-run)."""
        missing = [
            name
            for name, value in (
                ("SMTP_HOST", self.smtp_host),
                ("MAIL_FROM", self.mail_from),
                ("RENEWAL_URL", self.renewal_url),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Envoi réel impossible, variables manquantes : " + ", ".join(missing)
            )
