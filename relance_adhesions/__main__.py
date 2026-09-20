"""Permet l'exécution via `python -m relance_adhesions`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
