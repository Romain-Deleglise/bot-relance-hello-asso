"""Tests de l'anti-doublon et du rendu des mails."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relance_adhesions.config import Config  # noqa: E402
from relance_adhesions.mailer import MailRenderer, SmtpMailer  # noqa: E402
from relance_adhesions.membership import (  # noqa: E402
    STAGE_EXPIRATION,
    STAGE_PREAVIS,
    Membership,
    Reminder,
)
from relance_adhesions.state import ReminderStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def make_membership():
    return Membership(
        item_id=42, order_id=7, order_date=date(2025, 3, 10), end_date=date(2026, 3, 10),
        first_name="Jean", last_name="Dupont", email="jean@example.org",
        tier_name="Adhésion annuelle", form_slug="adhesion", form_type="Membership",
        validity_type="MovingYear",
    )


def make_reminder(stage=STAGE_PREAVIS):
    return Reminder(make_membership(), stage)


def make_config():
    return Config(
        helloasso_client_id="id", helloasso_client_secret="secret",
        helloasso_organization_slug="pause-ia",
        association_name="Pause IA", renewal_url="https://example.org/adhesion",
        mail_from="adhesions@example.org", mail_from_name="Pause IA",
        mail_subject="Votre adhésion à $association arrive à échéance",
        template_text=str(ROOT / "templates/relance.txt"),
        template_html=str(ROOT / "templates/relance.html"),
        template_text_expiration=str(ROOT / "templates/relance-expiration.txt"),
        template_html_expiration=str(ROOT / "templates/relance-expiration.html"),
        mail_subject_expiration="Votre adhésion à $association expire aujourd'hui",
    )


def test_store_anti_doublon(tmp_path):
    with ReminderStore(str(tmp_path / "state.sqlite3")) as store:
        preavis = make_reminder(STAGE_PREAVIS)
        membership = preavis.membership
        assert not store.already_sent(preavis.dedup_key)
        store.mark_sent(preavis.dedup_key, membership.item_id,
                        membership.email, membership.end_date)
        assert store.already_sent(preavis.dedup_key)
        # Le préavis envoyé ne bloque pas le mail du jour d'expiration.
        assert not store.already_sent(make_reminder(STAGE_EXPIRATION).dedup_key)
        # Une nouvelle échéance produit une nouvelle clé : relance possible.
        assert not store.already_sent("42:2027-03-10:preavis")


def test_store_journal_execution(tmp_path):
    with ReminderStore(str(tmp_path / "state.sqlite3")) as store:
        run_id = store.start_run(dry_run=True)
        store.finish_run(run_id, analysed=10, selected=3, sent=3, errors=0)
        row = store._conn.execute(
            "SELECT * FROM executions WHERE id = ?", (run_id,)
        ).fetchone()
        assert row["analysed"] == 10 and row["sent"] == 3 and row["dry_run"] == 1


def test_rendu_du_mail():
    rendered = MailRenderer(make_config()).render(make_reminder())
    assert rendered.subject == "Votre adhésion à Pause IA arrive à échéance"
    assert "Jean Dupont" in rendered.text
    assert "10/03/2026" in rendered.text
    assert "https://example.org/adhesion" in rendered.text
    assert rendered.html and "Renouveler mon adhésion" in rendered.html
    # Aucune variable de template non substituée.
    assert "$" not in rendered.text


def test_les_deux_etapes_ont_des_textes_distincts():
    renderer = MailRenderer(make_config())
    preavis = renderer.render(make_reminder(STAGE_PREAVIS))
    expiration = renderer.render(make_reminder(STAGE_EXPIRATION))
    assert preavis.subject != expiration.subject
    assert preavis.text != expiration.text
    assert "aujourd'hui" in expiration.subject
    assert "$" not in expiration.text


def test_construction_du_message():
    config = make_config()
    reminder = make_reminder()
    membership = reminder.membership
    message = SmtpMailer(config).build_message(
        membership, MailRenderer(config).render(reminder)
    )
    assert message["To"] == "Jean Dupont <jean@example.org>"
    assert message["From"] == "Pause IA <adhesions@example.org>"
    assert message.is_multipart()


def test_message_porte_les_entetes_de_delivrabilite():
    """Message-ID, Date et List-Unsubscribe présents et cohérents."""
    config = make_config()
    reminder = make_reminder()
    message = SmtpMailer(config).build_message(
        reminder.membership, MailRenderer(config).render(reminder)
    )
    assert message["Date"]
    message_id = message["Message-ID"]
    assert message_id and message_id.startswith("<") and "example.org>" in message_id
    # À défaut d'adresse dédiée, List-Unsubscribe retombe sur l'expéditeur.
    assert message["List-Unsubscribe"] == "<mailto:adhesions@example.org?subject=Desabonnement>"


def test_list_unsubscribe_utilise_l_adresse_dediee_si_fournie():
    config = make_config()
    config.unsubscribe_email = "stop@example.org"
    message = SmtpMailer(config).build_message(
        make_membership(), MailRenderer(config).render(make_reminder())
    )
    assert message["List-Unsubscribe"] == "<mailto:stop@example.org?subject=Desabonnement>"


def test_smtp_reessaie_sur_rejet_temporaire_puis_reussit():
    """Un rejet 4xx (greylisting) est réessayé ; l'envoi finit par passer."""
    import smtplib

    config = make_config()
    config.smtp_retry_attempts = 3
    config.smtp_retry_delay_seconds = 0  # pas d'attente réelle en test
    config.smtp_delay_seconds = 0

    class FlakyServer:
        def __init__(self):
            self.calls = 0

        def send_message(self, message):
            self.calls += 1
            if self.calls == 1:
                raise smtplib.SMTPResponseException(451, b"greylisted, try again")

    mailer = SmtpMailer(config)
    server = FlakyServer()
    mailer._connect = lambda: server  # court-circuite la vraie connexion
    mailer.send(make_reminder(), MailRenderer(config).render(make_reminder()))
    assert server.calls == 2  # un échec temporaire, puis succès


def test_smtp_ne_reessaie_pas_un_rejet_definitif():
    """Un rejet 5xx (adresse invalide) échoue immédiatement, sans retry."""
    import smtplib

    from relance_adhesions.mailer import MailError

    config = make_config()
    config.smtp_retry_delay_seconds = 0
    config.smtp_delay_seconds = 0

    class RejectingServer:
        def __init__(self):
            self.calls = 0

        def send_message(self, message):
            self.calls += 1
            raise smtplib.SMTPResponseException(550, b"mailbox unavailable")

    mailer = SmtpMailer(config)
    server = RejectingServer()
    mailer._connect = lambda: server
    try:
        mailer.send(make_reminder(), MailRenderer(config).render(make_reminder()))
    except MailError:
        assert server.calls == 1
        return
    raise AssertionError("MailError attendue sur un rejet 5xx")


def test_dry_run_ecrit_les_mails_sur_disque(tmp_path):
    """--dump-dir doit produire un .eml, un .txt et un .html relisibles."""
    from relance_adhesions.mailer import DryRunMailer

    config = make_config()
    reminder = make_reminder()
    rendered = MailRenderer(config).render(reminder)

    with DryRunMailer(config, str(tmp_path / "mails")) as mailer:
        mailer.send(reminder, rendered)

    produits = sorted(p.name for p in (tmp_path / "mails").iterdir())
    assert produits == [
        "001-preavis-jean@example.org.eml",
        "001-preavis-jean@example.org.html",
        "001-preavis-jean@example.org.txt",
    ]
    texte = (tmp_path / "mails" / "001-preavis-jean@example.org.txt").read_text(
        encoding="utf-8"
    )
    assert "Jean Dupont" in texte and "10/03/2026" in texte
    eml = (tmp_path / "mails" / "001-preavis-jean@example.org.eml").read_text(
        encoding="utf-8"
    )
    assert "To: Jean Dupont <jean@example.org>" in eml


def test_dry_run_sans_dump_dir_n_ecrit_rien(tmp_path):
    from relance_adhesions.mailer import DryRunMailer

    config = make_config()
    reminder = make_reminder()
    with DryRunMailer(config) as mailer:
        mailer.send(reminder, MailRenderer(config).render(reminder))
    assert list(tmp_path.iterdir()) == []
