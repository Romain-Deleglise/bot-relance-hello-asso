"""Tests de la logique métier (aucun appel réseau)."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relance_adhesions.membership import (  # noqa: E402
    STAGE_EXPIRATION,
    STAGE_PREAVIS,
    Reminder,
    add_one_year,
    compute_end_date,
    dedupe_memberships,
    normalize_item,
    parse_date,
    parse_local_date,
    select_to_remind,
    today_in,
)


def make_item(item_id=1, order_date="2025-03-10T09:00:00Z", email="a@example.org", **kw):
    item = {
        "id": item_id,
        "name": "Adhésion annuelle",
        "state": "Processed",
        "order": {"id": 100 + item_id, "date": order_date, "formSlug": "adhesion",
                  "formType": "Membership"},
        "payer": {"email": email, "firstName": "Jean", "lastName": "Dupont"},
        "user": {"firstName": "Jean", "lastName": "Dupont"},
    }
    item.update(kw)
    return item


def test_parse_date_gere_le_suffixe_z():
    assert parse_date("2025-03-10T09:00:00Z") == date(2025, 3, 10)
    assert parse_date("2025-03-10T09:00:00.1234567+01:00") == date(2025, 3, 10)
    assert parse_date(None) is None
    assert parse_date("pas une date") is None


def test_add_one_year_gere_le_29_fevrier():
    assert add_one_year(date(2024, 2, 29)) == date(2025, 2, 28)
    assert add_one_year(date(2025, 3, 10)) == date(2026, 3, 10)


def test_compute_end_date_par_type_de_validite():
    order = date(2025, 3, 10)
    assert compute_end_date(order, "MovingYear", None, 365) == date(2026, 3, 10)
    assert compute_end_date(order, "MovingYear", None, 180) == date(2025, 9, 6)
    assert compute_end_date(order, "Custom", date(2025, 12, 31), 365) == date(2025, 12, 31)
    # Custom sans endDate exploitable : repli sur l'année glissante.
    assert compute_end_date(order, "Custom", None, 365) == date(2026, 3, 10)
    assert compute_end_date(order, "Illimited", None, 365) is None


def test_normalize_item_utilise_l_email_du_payeur():
    membership = normalize_item(make_item(), "MovingYear", None, 365)
    assert membership is not None
    assert membership.email == "a@example.org"
    assert membership.full_name == "Jean Dupont"
    assert membership.end_date == date(2026, 3, 10)
    assert Reminder(membership, STAGE_PREAVIS).dedup_key == "1:2026-03-10:preavis"


def test_normalize_item_repli_sur_un_champ_personnalise():
    item = make_item(email="")
    item["customFields"] = [{"name": "E-mail de l'adhérent", "answer": "b@example.org"}]
    membership = normalize_item(item, "MovingYear", None, 365)
    assert membership is not None and membership.email == "b@example.org"


def test_normalize_item_ignore_sans_email():
    assert normalize_item(make_item(email=""), "MovingYear", None, 365) is None


def test_normalize_item_ignore_sans_date_de_commande():
    assert normalize_item(make_item(order_date=None), "MovingYear", None, 365) is None


def test_dedupe_garde_la_plus_recente_pour_une_meme_personne():
    """Option (c) : mêmes e-mail et nom = même personne, on garde la récente."""
    ancienne = normalize_item(make_item(1, "2023-03-10T09:00:00Z"), "MovingYear", None, 365)
    recente = normalize_item(make_item(2, "2025-03-10T09:00:00Z"), "MovingYear", None, 365)
    retenues = dedupe_memberships([ancienne, recente])
    assert [m.item_id for m in retenues] == [2]


def test_dedupe_distingue_deux_personnes_partageant_une_adresse():
    """Un même payeur pour deux personnes : chacune doit être relancée."""
    parent = normalize_item(
        make_item(1, "2025-03-10T09:00:00Z", email="foyer@example.org"),
        "MovingYear", None, 365,
    )
    enfant_item = make_item(2, "2025-03-11T09:00:00Z", email="foyer@example.org")
    enfant_item["user"] = {"firstName": "Léa", "lastName": "Dupont"}
    enfant = normalize_item(enfant_item, "MovingYear", None, 365)
    retenues = dedupe_memberships([parent, enfant])
    assert {m.item_id for m in retenues} == {1, 2}


def test_dedupe_fusionne_malgre_la_casse_et_les_espaces_du_nom():
    a = make_item(1, "2024-03-10T09:00:00Z")
    a["user"] = {"firstName": "Jean", "lastName": "DUPONT"}
    b = make_item(2, "2025-03-10T09:00:00Z")
    b["user"] = {"firstName": "jean", "lastName": "dupont"}
    retenues = dedupe_memberships(
        [normalize_item(a, "MovingYear", None, 365),
         normalize_item(b, "MovingYear", None, 365)]
    )
    assert [m.item_id for m in retenues] == [2]


def test_parse_local_date_utilise_le_fuseau_de_l_association():
    """Une commande à 23 h 30 heure de Paris est datée du bon jour, pas de la veille."""
    # 2025-06-10T22:30:00Z = 2025-06-11 00:30 à Paris (UTC+2 en été).
    assert parse_local_date("2025-06-10T22:30:00Z", "Europe/Paris") == date(2025, 6, 11)
    # En UTC, la même valeur retombe sur la veille.
    assert parse_date("2025-06-10T22:30:00Z") == date(2025, 6, 10)


def test_today_in_renvoie_une_date():
    assert isinstance(today_in("Europe/Paris"), date)
    # Fuseau inconnu : repli silencieux, pas d'exception.
    assert isinstance(today_in("Zone/Inexistante"), date)


def test_resolve_timezone_retombe_sur_utc_sans_base_de_fuseaux(monkeypatch):
    """Sur une image sans tzdata, ZoneInfo échoue pour tout nom : repli UTC."""
    from datetime import timezone

    from relance_adhesions import membership as m

    def boom(*_a, **_k):
        raise m.ZoneInfoNotFoundError("no tzdata")

    monkeypatch.setattr(m, "ZoneInfo", boom)
    assert m.resolve_timezone("Europe/Paris") == timezone.utc
    # Le calcul de dates continue de fonctionner malgré tout.
    assert isinstance(m.today_in("Europe/Paris"), date)


def test_select_to_remind_deux_etapes():
    """Préavis avant l'échéance, second mail le jour de l'expiration."""
    today = date(2026, 3, 1)
    dans_9_jours = normalize_item(make_item(1, "2025-03-10T09:00:00Z"), "MovingYear", None, 365)
    dans_6_mois = normalize_item(make_item(2, "2025-09-10T09:00:00Z"), "MovingYear", None, 365)
    expire_aujourdhui = normalize_item(make_item(3, "2025-03-01T09:00:00Z"), "MovingYear", None, 365)
    expiree_hier = normalize_item(make_item(4, "2025-02-28T09:00:00Z"), "MovingYear", None, 365)
    illimitee = normalize_item(make_item(5, "2025-03-10T09:00:00Z"), "Illimited", None, 365)
    toutes = [dans_9_jours, dans_6_mois, expire_aujourdhui, expiree_hier, illimitee]

    relances = select_to_remind(toutes, today, days_before=15, days_after=0)
    obtenu = {(r.membership.item_id, r.stage) for r in relances}
    assert obtenu == {(1, STAGE_PREAVIS), (3, STAGE_EXPIRATION)}


def test_une_adhesion_ne_declenche_pas_les_deux_etapes_le_meme_jour():
    """Les deux fenêtres sont disjointes : jamais deux mails le même jour."""
    today = date(2026, 3, 1)
    expire_aujourdhui = normalize_item(
        make_item(1, "2025-03-01T09:00:00Z"), "MovingYear", None, 365
    )
    relances = select_to_remind([expire_aujourdhui], today, days_before=15, days_after=7)
    assert [r.stage for r in relances] == [STAGE_EXPIRATION]


def test_rattrapage_des_adhesions_recemment_expirees():
    today = date(2026, 3, 1)
    expiree_il_y_a_3_jours = normalize_item(
        make_item(1, "2025-02-26T09:00:00Z"), "MovingYear", None, 365
    )
    assert select_to_remind([expiree_il_y_a_3_jours], today, 15, 0) == []
    relances = select_to_remind([expiree_il_y_a_3_jours], today, 15, 7)
    assert [r.stage for r in relances] == [STAGE_EXPIRATION]


def test_les_deux_relances_ont_des_cles_anti_doublon_distinctes():
    """C'est ce qui permet au second mail de partir malgré le premier."""
    membership = normalize_item(make_item(1), "MovingYear", None, 365)
    preavis = Reminder(membership, STAGE_PREAVIS)
    expiration = Reminder(membership, STAGE_EXPIRATION)
    assert preavis.dedup_key != expiration.dedup_key
