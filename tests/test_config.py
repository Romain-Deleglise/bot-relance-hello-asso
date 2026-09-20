"""Tests du chargement de configuration (.env)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relance_adhesions.config import load_env_file  # noqa: E402


def test_load_env_retire_un_commentaire_de_fin_de_ligne(tmp_path, monkeypatch):
    """Un commentaire collé par erreur après la valeur ne doit pas la polluer."""
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("MAIL_FROM", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "SMTP_HOST=email-smtp.eu-west-1.amazonaws.com     # ← host de CiviCRM\n"
        "MAIL_FROM=contact@pauseia.fr\n",
        encoding="utf-8",
    )
    load_env_file(env)
    assert os.environ["SMTP_HOST"] == "email-smtp.eu-west-1.amazonaws.com"
    assert os.environ["MAIL_FROM"] == "contact@pauseia.fr"


def test_load_env_preserve_un_diese_dans_une_valeur_quotee(tmp_path, monkeypatch):
    """Un mot de passe contenant « # » reste intact s'il est entre guillemets."""
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    env = tmp_path / ".env"
    env.write_text('SMTP_PASSWORD="ab #cd#ef"\n', encoding="utf-8")
    load_env_file(env)
    assert os.environ["SMTP_PASSWORD"] == "ab #cd#ef"


def test_load_env_preserve_un_diese_colle_sans_espace(tmp_path, monkeypatch):
    """Un « # » collé à la valeur (sans espace avant) n'est pas un commentaire."""
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    env = tmp_path / ".env"
    env.write_text("SMTP_PASSWORD=ab#cd#ef\n", encoding="utf-8")
    load_env_file(env)
    assert os.environ["SMTP_PASSWORD"] == "ab#cd#ef"
