"""Tests de la liste d'exclusion (désinscription)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relance_adhesions.suppression import add_suppressed, load_suppressed  # noqa: E402


def test_load_absent_renvoie_vide(tmp_path):
    assert load_suppressed(str(tmp_path / "rien.txt")) == set()


def test_add_puis_load(tmp_path):
    fichier = str(tmp_path / "desinscrits.txt")
    assert add_suppressed(fichier, "Jean@Example.ORG") is True
    # Normalisation en minuscules.
    assert load_suppressed(fichier) == {"jean@example.org"}
    # Doublon : non réajouté.
    assert add_suppressed(fichier, "jean@example.org") is False


def test_load_ignore_lignes_vides_et_commentaires(tmp_path):
    fichier = tmp_path / "desinscrits.txt"
    fichier.write_text(
        "# désinscrits\n\na@example.org\n  b@example.org  \n", encoding="utf-8"
    )
    assert load_suppressed(str(fichier)) == {"a@example.org", "b@example.org"}


def test_add_adresse_vide_leve(tmp_path):
    try:
        add_suppressed(str(tmp_path / "d.txt"), "   ")
    except ValueError:
        return
    raise AssertionError("ValueError attendue pour une adresse vide")
