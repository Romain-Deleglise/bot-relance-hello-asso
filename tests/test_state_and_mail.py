"""Tests de l'anti-doublon et du rendu des mails."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relance_adhesions.config import Config  # noqa: E402
from relance_adhesions.mailer import MailRenderer, SmtpMailer  # noqa: E402
from relance_adhesions.membership import Membership  # noqa: E402
from relance_adhesions.state import ReminderStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def make_membership():
    return Membership(
        item_id=42, order_id=7, order_date=date(2025, 3, 10), end_date=date(2026, 3, 10),
        first_name="Jean", last_name="Dupont", email="jean@example.org",
        tier_name="Adhésion annuelle", form_slug="adhesion", form_type="Membership",
        validity_type="MovingYear",
    )


def make_config():
    return Config(
        helloasso_client_id="id", helloasso_client_secret="secret",
        helloasso_organization_slug="pause-ia",
        association_name="Pause IA", renewal_url="https://example.org/adhesion",
        mail_from="adhesions@example.org", mail_from_name="Pause IA",
        mail_subject="Votre adhésion à $association arrive à échéance",
        template_text=str(ROOT / "templates/relance.txt"),
        template_html=str(ROOT / "templates/relance.html"),
    )


def test_store_anti_doublon(tmp_path):
    with ReminderStore(str(tmp_path / "state.sqlite3")) as store:
        membership = make_membership()
        assert not store.already_sent(membership.dedup_key)
        store.mark_sent(membership.dedup_key, membership.item_id,
                        membership.email, membership.end_date)
        assert store.already_sent(membership.dedup_key)
        # Une nouvelle échéance produit une nouvelle clé : relance possible.
        assert not store.already_sent("42:2027-03-10")


def test_store_journal_execution(tmp_path):
    with ReminderStore(str(tmp_path / "state.sqlite3")) as store:
        run_id = store.start_run(dry_run=True)
        store.finish_run(run_id, analysed=10, selected=3, sent=3, errors=0)
        row = store._conn.execute(
            "SELECT * FROM executions WHERE id = ?", (run_id,)
        ).fetchone()
        assert row["analysed"] == 10 and row["sent"] == 3 and row["dry_run"] == 1


def test_rendu_du_mail():
    rendered = MailRenderer(make_config()).render(make_membership())
    assert rendered.subject == "Votre adhésion à Pause IA arrive à échéance"
    assert "Jean Dupont" in rendered.text
    assert "10/03/2026" in rendered.text
    assert "https://example.org/adhesion" in rendered.text
    assert rendered.html and "Renouveler mon adhésion" in rendered.html
    # Aucune variable de template non substituée.
    assert "$" not in rendered.text


def test_construction_du_message():
    config = make_config()
    membership = make_membership()
    message = SmtpMailer(config).build_message(
        membership, MailRenderer(config).render(membership)
    )
    assert message["To"] == "Jean Dupont <jean@example.org>"
    assert message["From"] == "Pause IA <adhesions@example.org>"
    assert message.is_multipart()
