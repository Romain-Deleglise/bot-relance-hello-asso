"""Normalisation des items HelloAsso et calcul des dates d'échéance.

C'est le cœur du projet : **l'API HelloAsso n'expose pas de date de fin
d'adhésion sur l'item**. Le modèle `Item` (`/organizations/{slug}/items`)
contient `order.date`, `payer`, `user`, `name`, `state`, `tierDescription`,
`customFields` — mais aucune échéance.

La règle d'échéance est portée par le **formulaire d'adhésion**, dont le détail
public expose `validityType` :

* `MovingYear`  → année glissante : fin = date de commande + durée configurée
                  (12 mois par défaut, cf. `RELANCE_MEMBERSHIP_DURATION_DAYS`).
* `Custom`      → période fixe définie sur le formulaire : fin = `endDate`
                  du formulaire (identique pour tous les adhérents).
* `Illimited`   → adhésion sans échéance : jamais relancée.

Si le formulaire ne peut pas être interrogé (droits, formulaire supprimé), on
retombe sur l'année glissante, qui est le cas de figure le plus courant, et on
le signale dans les logs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .helloasso import VALIDITY_CUSTOM, VALIDITY_ILLIMITED, VALIDITY_MOVING_YEAR

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Fuseau de référence pour toute la logique de dates. HelloAsso renvoie les
# dates de commande en UTC ; les convertir dans le fuseau de l'association avant
# d'en extraire le jour évite qu'une commande passée tard le soir (heure de
# Paris) ne soit datée de la veille, ce qui décalerait l'échéance d'un jour.
DEFAULT_TIMEZONE = "Europe/Paris"


def resolve_timezone(name: str | None) -> tzinfo:
    """Renvoie le fuseau demandé. Ne lève jamais : repli Paris puis UTC.

    Sur un hôte sans base de fuseaux (image Docker « slim » sans `tzdata`),
    `ZoneInfo` échoue pour *tout* nom, y compris Europe/Paris. On retombe alors
    sur UTC plutôt que de laisser l'exception interrompre l'exécution — mieux
    vaut un calcul de dates à ±2 h qu'un bot qui ne tourne pas du tout.
    """
    requested = name or DEFAULT_TIMEZONE
    for candidate in (requested, DEFAULT_TIMEZONE):
        try:
            zone = ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        if candidate != requested:
            logger.warning(
                "Fuseau horaire %r inconnu, repli sur %s", requested, candidate
            )
        return zone
    logger.warning(
        "Aucune base de fuseaux disponible (tzdata manquant ?), repli sur UTC"
    )
    return timezone.utc


def today_in(tz: str | None) -> date:
    """Date du jour dans le fuseau de l'association (et non celui du serveur)."""
    return datetime.now(resolve_timezone(tz)).date()


@dataclass
class Membership:
    """Une adhésion normalisée, prête à être filtrée puis relancée."""

    item_id: int
    order_id: int | None
    order_date: date
    end_date: date | None
    first_name: str
    last_name: str
    email: str
    tier_name: str
    form_slug: str
    form_type: str
    validity_type: str

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part).strip()

    @property
    def display_name(self) -> str:
        """Nom affiché dans le mail, avec repli neutre si l'API n'a rien fourni."""
        return self.full_name or "cher adhérent, chère adhérente"



# Étapes de relance. Chaque adhésion en reçoit au plus une de chaque type.
STAGE_PREAVIS = "preavis"        # quelques jours avant l'échéance
STAGE_EXPIRATION = "expiration"  # le jour de l'expiration

STAGE_LABELS = {
    STAGE_PREAVIS: "préavis",
    STAGE_EXPIRATION: "jour d'expiration",
}


@dataclass
class Reminder:
    """Une relance à envoyer : une adhésion, à une étape donnée."""

    membership: Membership
    stage: str

    @property
    def dedup_key(self) -> str:
        """Clé d'anti-doublon : une relance par adhésion, échéance *et* étape.

        Inclure la date de fin garantit qu'une personne ayant renouvelé sera
        bien relancée l'année suivante, pour sa nouvelle échéance. Inclure
        l'étape permet les deux relances successives sans que la première
        n'empêche la seconde.
        """
        end = (
            self.membership.end_date.isoformat()
            if self.membership.end_date
            else "none"
        )
        return f"{self.membership.item_id}:{end}:{self.stage}"

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage, self.stage)


def parse_datetime(value: Any) -> datetime | None:
    """Parse une date ISO 8601 HelloAsso en datetime *aware* (UTC si naïve)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    # `fromisoformat` de Python < 3.11 ne gère pas le suffixe "Z" ni les
    # fractions de seconde à plus de 6 chiffres.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        logger.debug("Date HelloAsso non interprétable : %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_date(value: Any) -> date | None:
    """Jour d'une date ISO 8601, en UTC (suffisant pour une date sans heure)."""
    parsed = parse_datetime(value)
    return parsed.date() if parsed else None


def parse_local_date(value: Any, tz: str | None = DEFAULT_TIMEZONE) -> date | None:
    """Jour d'une date/heure ISO 8601, exprimé dans le fuseau `tz`.

    À utiliser pour la date de commande, dont l'heure compte : une commande à
    23 h 30 heure de Paris est datée du bon jour, pas de la veille en UTC.
    """
    parsed = parse_datetime(value)
    return parsed.astimezone(resolve_timezone(tz)).date() if parsed else None


def add_one_year(start: date) -> date:
    """Ajoute un an en gérant le 29 février (→ 28 février l'année suivante)."""
    try:
        return start.replace(year=start.year + 1)
    except ValueError:
        return start.replace(year=start.year + 1, month=2, day=28)


def compute_end_date(
    order_date: date,
    validity_type: str,
    form_end_date: date | None,
    duration_days: int,
) -> date | None:
    """Calcule la date de fin d'adhésion. `None` = adhésion sans échéance."""
    if validity_type == VALIDITY_ILLIMITED:
        return None
    if validity_type == VALIDITY_CUSTOM:
        # Période fixe portée par le formulaire (ex. année civile).
        # Sans `endDate` exploitable, on retombe sur l'année glissante.
        return form_end_date or add_one_year(order_date)
    # MovingYear (défaut) : année glissante depuis la date de commande.
    if duration_days == 365:
        return add_one_year(order_date)
    return order_date + timedelta(days=duration_days)


def _extract_email(item: dict[str, Any]) -> str:
    """Récupère l'e-mail de l'adhérent.

    HelloAsso place l'e-mail sur le **payeur** (`payer.email`). L'objet `user`
    (l'adhérent nommé sur l'item) ne contient que nom et prénom. Quand
    l'adhésion est payée pour un tiers, l'e-mail de l'adhérent est souvent
    saisi dans un champ personnalisé : on le cherche en repli.
    """
    payer_email = ((item.get("payer") or {}).get("email") or "").strip()
    if EMAIL_RE.match(payer_email):
        return payer_email

    for field in item.get("customFields") or []:
        answer = (field.get("answer") or "").strip()
        name = (field.get("name") or "").lower()
        if EMAIL_RE.match(answer) and ("mail" in name or field.get("type") == "Email"):
            return answer
    return ""


def normalize_item(
    item: dict[str, Any],
    validity_type: str,
    form_end_date: date | None,
    duration_days: int,
    tz: str | None = DEFAULT_TIMEZONE,
) -> Membership | None:
    """Transforme un item HelloAsso en `Membership`. `None` si inexploitable."""
    order = item.get("order") or {}
    order_date = parse_local_date(order.get("date"), tz)
    if order_date is None:
        logger.warning("Item %s ignoré : date de commande absente", item.get("id"))
        return None

    email = _extract_email(item)
    if not email:
        logger.warning(
            "Item %s ignoré : aucune adresse e-mail exploitable", item.get("id")
        )
        return None

    # `user` = l'adhérent désigné sur l'item ; à défaut, le payeur.
    user = item.get("user") or {}
    payer = item.get("payer") or {}
    first_name = (user.get("firstName") or payer.get("firstName") or "").strip()
    last_name = (user.get("lastName") or payer.get("lastName") or "").strip()

    return Membership(
        item_id=int(item.get("id") or 0),
        order_id=order.get("id"),
        order_date=order_date,
        end_date=compute_end_date(order_date, validity_type, form_end_date, duration_days),
        first_name=first_name,
        last_name=last_name,
        email=email,
        tier_name=(item.get("name") or "").strip(),
        form_slug=(order.get("formSlug") or "").strip(),
        form_type=(order.get("formType") or "Membership").strip(),
        validity_type=validity_type,
    )


def _person_key(membership: Membership) -> tuple[str, str]:
    """Identité d'un adhérent : e-mail + nom normalisés (option c).

    Le nom est mis en minuscules et ses espaces multiples réduits, pour que
    « Jean  DUPONT » et « jean dupont » désignent bien la même personne.
    """
    name = re.sub(r"\s+", " ", membership.full_name).strip().lower()
    return (membership.email.lower(), name)


def dedupe_memberships(memberships: list[Membership]) -> list[Membership]:
    """Ne conserve que l'adhésion la plus récente par personne — option (c).

    La clé est `(e-mail, nom)`, et non l'e-mail seul :

    * deux personnes partageant une même adresse (un même payeur réglant pour un
      couple ou un enfant) sont **distinguées** et relancées chacune ;
    * les adhésions successives d'une **même** personne sont fusionnées : seule
      la plus récente est gardée. Un adhérent qui renouvelle — même en avance —
      n'est donc jamais relancé pour son ancienne échéance déjà remplacée.

    Compromis assumé : deux personnes homonymes partageant une adresse (rare)
    seraient fusionnées. C'est préférable au risque inverse (relancer quelqu'un
    qui vient de renouveler), qui serait perçu comme une erreur.
    """
    latest: dict[tuple[str, str], Membership] = {}
    for membership in memberships:
        key = _person_key(membership)
        current = latest.get(key)
        if current is None or membership.order_date > current.order_date:
            latest[key] = membership
    return sorted(
        latest.values(), key=lambda m: (m.end_date or date.max, m.email, m.item_id)
    )


def select_to_remind(
    memberships: list[Membership],
    today: date,
    days_before: int,
    days_after: int,
) -> list[Reminder]:
    """Retient les relances à envoyer, réparties en deux étapes distinctes.

    * `preavis`     : l'échéance est à venir, dans les `days_before` jours —
                      fenêtre `]today ; today + days_before]`.
    * `expiration`  : l'échéance est atteinte ou tout juste dépassée —
                      fenêtre `[today - days_after ; today]`.

    Les deux fenêtres sont disjointes par construction (la date du jour
    appartient à la seconde), de sorte qu'une même adhésion ne peut pas
    déclencher les deux relances le même jour.
    """
    preavis_end = today + timedelta(days=days_before)
    expiration_start = today - timedelta(days=days_after)

    reminders: list[Reminder] = []
    for membership in memberships:
        end_date = membership.end_date
        if end_date is None:
            continue
        if today < end_date <= preavis_end:
            reminders.append(Reminder(membership, STAGE_PREAVIS))
        elif expiration_start <= end_date <= today:
            reminders.append(Reminder(membership, STAGE_EXPIRATION))

    # Les échéances les plus urgentes d'abord.
    return sorted(reminders, key=lambda r: (r.membership.end_date, r.membership.email))
