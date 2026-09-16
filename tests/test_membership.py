"""Tests de la logique métier (aucun appel réseau)."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relance_adhesions.membership import (  # noqa: E402
    add_one_year,
    compute_end_date,
    keep_latest_per_member,
    normalize_item,
    parse_date,
    select_to_remind,
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
    assert membership.dedup_key == "1:2026-03-10"


def test_normalize_item_repli_sur_un_champ_personnalise():
    item = make_item(email="")
    item["customFields"] = [{"name": "E-mail de l'adhérent", "answer": "b@example.org"}]
    membership = normalize_item(item, "MovingYear", None, 365)
    assert membership is not None and membership.email == "b@example.org"


def test_normalize_item_ignore_sans_email():
    assert normalize_item(make_item(email=""), "MovingYear", None, 365) is None


def test_normalize_item_ignore_sans_date_de_commande():
    assert normalize_item(make_item(order_date=None), "MovingYear", None, 365) is None


def test_keep_latest_per_member_deduplique_par_email():
    ancienne = normalize_item(make_item(1, "2023-03-10T09:00:00Z"), "MovingYear", None, 365)
    recente = normalize_item(make_item(2, "2025-03-10T09:00:00Z"), "MovingYear", None, 365)
    retenues = keep_latest_per_member([ancienne, recente])
    assert [m.item_id for m in retenues] == [2]


def test_select_to_remind_fenetre():
    today = date(2026, 3, 1)
    dans_9_jours = normalize_item(make_item(1, "2025-03-10T09:00:00Z"), "MovingYear", None, 365)
    dans_6_mois = normalize_item(make_item(2, "2025-09-10T09:00:00Z"), "MovingYear", None, 365)
    expiree_hier = normalize_item(make_item(3, "2025-02-28T09:00:00Z"), "MovingYear", None, 365)
    illimitee = normalize_item(make_item(4, "2025-03-10T09:00:00Z"), "Illimited", None, 365)
    toutes = [dans_9_jours, dans_6_mois, expiree_hier, illimitee]

    sans_rattrapage = select_to_remind(toutes, today, days_before=15, days_after=0)
    assert [m.item_id for m in sans_rattrapage] == [1]

    avec_rattrapage = select_to_remind(toutes, today, days_before=15, days_after=7)
    assert sorted(m.item_id for m in avec_rattrapage) == [1, 3]
