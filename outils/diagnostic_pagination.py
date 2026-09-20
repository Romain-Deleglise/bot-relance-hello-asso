"""Diagnostic de la pagination de l'API HelloAsso.

Interroge l'endpoint des adhésions page par page et affiche **uniquement des
informations non personnelles** : objet `pagination` brut, nombre d'éléments
reçus, identifiants et dates de commande. Aucun nom ni adresse e-mail n'est
affiché, de sorte que la sortie puisse être partagée pour analyse.

Usage :
    .venv/bin/python -m outils.diagnostic_pagination
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relance_adhesions.config import Config, load_env_file  # noqa: E402
from relance_adhesions.helloasso import HelloAssoClient  # noqa: E402


def resume(payload: dict) -> dict:
    """Extrait de quoi comprendre la pagination, sans donnée personnelle."""
    data = payload.get("data") or []
    dates = sorted(
        (item.get("order") or {}).get("date", "")[:10] for item in data if item
    )
    return {
        "nb_elements": len(data),
        "identifiants_min_max": (
            [min(i.get("id", 0) for i in data), max(i.get("id", 0) for i in data)]
            if data else []
        ),
        "dates_commande_min_max": [dates[0], dates[-1]] if dates else [],
        "pagination": payload.get("pagination"),
    }


def main() -> int:
    try:
        load_env_file(".env")
    except Exception:
        pass
    config = Config.from_env()
    client = HelloAssoClient(
        client_id=config.helloasso_client_id,
        client_secret=config.helloasso_client_secret,
        api_base=config.helloasso_api_base,
        auth_base=config.helloasso_auth_base,
    )
    slug = config.helloasso_organization_slug
    chemin = f"organizations/{slug}/items"

    base = {
        "tierTypes": ["Membership"],
        "itemStates": ["Processed", "Registered"],
        "pageSize": 100,
        "sortField": "Date",
        "sortOrder": "Asc",
    }

    print("=" * 72)
    print("A. Trois premières pages, par pageIndex, AVEC les filtres du bot")
    print("=" * 72)
    token = None
    for index in (1, 2, 3):
        params = dict(base, pageIndex=index)
        if token:
            params["continuationToken"] = token
        payload = client.get(chemin, params)
        info = resume(payload)
        print(f"\n--- pageIndex={index} " + ("(+ continuationToken)" if token else ""))
        print(json.dumps(info, indent=2, ensure_ascii=False))
        token = (payload.get("pagination") or {}).get("continuationToken")

    print("\n" + "=" * 72)
    print("B. Page 1 SANS filtre d'état ni de type (pour isoler leur effet)")
    print("=" * 72)
    for libelle, params in [
        ("sans itemStates", {"tierTypes": ["Membership"], "pageSize": 100, "pageIndex": 1}),
        ("sans aucun filtre", {"pageSize": 100, "pageIndex": 1}),
        ("pageSize=200", dict(base, pageSize=200, pageIndex=1)),
        ("pageSize=20", dict(base, pageSize=20, pageIndex=1)),
        ("pageSize=20, pageIndex=2", dict(base, pageSize=20, pageIndex=2)),
    ]:
        try:
            info = resume(client.get(chemin, params))
            print(f"\n--- {libelle}")
            print(json.dumps(info, indent=2, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(f"\n--- {libelle} : ERREUR {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
