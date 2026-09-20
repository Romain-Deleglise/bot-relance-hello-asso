"""Liste d'exclusion : adresses à ne jamais relancer (désinscription).

Mécanisme volontairement simple et opérable sans compétence technique : un
fichier texte, une adresse par ligne. Le bot écarte ces adresses de toute
relance. Une adresse s'ajoute soit à la main (une ligne dans le fichier), soit
via la commande `--unsubscribe adresse@exemple.fr`.

Le fichier vit dans un volume persistant (comme la base anti-doublon) ; les
lignes vides et celles commençant par « # » sont ignorées.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_suppressed(path: str) -> set[str]:
    """Charge l'ensemble des adresses désinscrites (minuscules). Vide si absent."""
    file = Path(path)
    if not file.is_file():
        return set()
    suppressed: set[str] = set()
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lower()
        if line and not line.startswith("#"):
            suppressed.add(line)
    return suppressed


def add_suppressed(path: str, email: str) -> bool:
    """Ajoute une adresse à la liste d'exclusion. Renvoie False si déjà présente."""
    normalized = email.strip().lower()
    if not normalized:
        raise ValueError("adresse vide")
    if normalized in load_suppressed(path):
        return False
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a", encoding="utf-8") as handle:
        handle.write(normalized + "\n")
    logger.info("Adresse ajoutée à la liste d'exclusion : %s", normalized)
    return True
