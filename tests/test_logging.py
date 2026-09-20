"""Tests de l'initialisation de la journalisation."""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relance_adhesions.cli import setup_logging  # noqa: E402


def test_cree_le_dossier_de_log_manquant(tmp_path):
    """Le dossier parent du fichier de log doit être créé automatiquement."""
    log_file = tmp_path / "logs" / "relance.log"
    assert not log_file.parent.exists()

    setup_logging("INFO", str(log_file))
    logging.getLogger("relance_adhesions").info("test")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_file.is_file()
    assert "test" in log_file.read_text(encoding="utf-8")
    logging.shutdown()


def test_dossier_inaccessible_ne_fait_pas_echouer(tmp_path):
    """Un chemin de log impossible se dégrade en console, sans exception."""
    barriere = tmp_path / "fichier"
    barriere.write_text("", encoding="utf-8")
    # `fichier` est un fichier : impossible d'y créer un sous-dossier.
    setup_logging("INFO", str(barriere / "sous-dossier" / "relance.log"))
    logging.getLogger("relance_adhesions").info("toujours vivant")
    logging.shutdown()
