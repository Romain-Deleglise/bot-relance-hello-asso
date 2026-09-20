"""Tests du client HelloAsso avec une session HTTP simulée (aucun réseau)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relance_adhesions.helloasso import (  # noqa: E402
    HelloAssoAuthError,
    HelloAssoClient,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    """Session requests minimale : rejoue des réponses préprogrammées."""

    def __init__(self, token_responses, get_responses):
        self.token_responses = list(token_responses)
        self.get_responses = list(get_responses)
        self.get_calls = []

    def post(self, url, **kwargs):
        return self.token_responses.pop(0)

    def get(self, url, params=None, **kwargs):
        self.get_calls.append((url, dict(params or {})))
        return self.get_responses.pop(0)


TOKEN_OK = FakeResponse(payload={
    "access_token": "tok", "refresh_token": "ref", "expires_in": 1800,
    "token_type": "bearer",
})


def make_client(session):
    return HelloAssoClient("id", "secret", session=session)


def test_authentification_echouee_leve_une_erreur_dediee():
    session = FakeSession([FakeResponse(401, text="invalid_client")], [])
    try:
        make_client(session).authenticate()
    except HelloAssoAuthError:
        return
    raise AssertionError("HelloAssoAuthError attendue")


def _page(ids, page_index, total_pages=None, token=None, total_count=None):
    pagination = {"pageIndex": page_index, "pageSize": 2}
    if total_pages is not None:
        pagination["totalPages"] = total_pages
    if token is not None:
        pagination["continuationToken"] = token
    if total_count is not None:
        pagination["totalCount"] = total_count
    return FakeResponse(payload={"data": [{"id": i} for i in ids],
                                 "pagination": pagination})


def test_pagination_par_continuation_token():
    session = FakeSession([TOKEN_OK], [
        _page([1, 2], 1, total_pages=2, token="abc"),
        _page([3], 2, total_pages=2),
    ])
    items = list(make_client(session).iter_membership_items("pause-ia", page_size=2))

    assert [item["id"] for item in items] == [1, 2, 3]
    assert session.get_calls[0][1]["tierTypes"] == ["Membership"]
    assert session.get_calls[0][1]["itemStates"] == ["Processed", "Registered"]
    assert session.get_calls[1][1]["continuationToken"] == "abc"
    assert session.get_calls[0][0].endswith("/organizations/pause-ia/items")


def test_pagination_poursuit_sans_total_pages():
    """Régression : `totalPages` absent ne doit pas faire croire à une page unique.

    C'est le bug qui tronquait la liste des adhérents à la première page.
    """
    session = FakeSession([TOKEN_OK], [
        _page([1, 2], 1, token="t1"),
        _page([3, 4], 2, token="t2"),
        _page([5], 3),
    ])
    items = list(make_client(session).iter_membership_items("pause-ia", page_size=2))
    assert [item["id"] for item in items] == [1, 2, 3, 4, 5]


def test_pagination_sans_token_retombe_sur_page_index():
    session = FakeSession([TOKEN_OK], [
        _page([1, 2], 1),
        _page([3, 4], 2),
        _page([], 3),
    ])
    items = list(make_client(session).iter_membership_items("pause-ia", page_size=2))
    assert [item["id"] for item in items] == [1, 2, 3, 4]
    assert session.get_calls[1][1]["pageIndex"] == 2
    assert session.get_calls[2][1]["pageIndex"] == 3


def test_pagination_s_arrete_sur_total_count():
    session = FakeSession([TOKEN_OK], [
        _page([1, 2], 1, token="t1", total_count=3),
        _page([3, 4], 2, token="t2", total_count=3),
    ])
    items = list(make_client(session).iter_membership_items("pause-ia", page_size=2))
    # `totalCount` atteint : on ne demande pas de page supplémentaire.
    assert [item["id"] for item in items] == [1, 2, 3, 4]
    assert len(session.get_calls) == 2


def test_pagination_ne_boucle_pas_sur_un_token_repete():
    session = FakeSession([TOKEN_OK], [
        _page([1, 2], 1, token="meme-token"),
        _page([3, 4], 1, token="meme-token"),
        _page([], 2),
    ])
    items = list(make_client(session).iter_membership_items("pause-ia", page_size=2))
    assert [item["id"] for item in items] == [1, 2, 3, 4]


def test_401_en_cours_de_route_declenche_une_reauthentification():
    session = FakeSession(
        [TOKEN_OK, TOKEN_OK],
        [
            FakeResponse(401, text="expired"),
            FakeResponse(payload={"data": [{"id": 1}],
                                  "pagination": {"pageIndex": 1, "pageSize": 100,
                                                 "totalPages": 1}}),
        ],
    )
    items = list(make_client(session).iter_membership_items("pause-ia"))
    assert [item["id"] for item in items] == [1]
    assert session.token_responses == []  # deux authentifications consommées


def test_total_count_negatif_ne_stoppe_pas_la_pagination():
    """Régression : HelloAsso renvoie totalCount = -1 quand le total est inconnu.

    Traiter -1 comme un total réel arrêtait la pagination dès la première page.
    """
    session = FakeSession([TOKEN_OK], [
        _page([1, 2], 1, token="t1", total_count=-1),
        _page([3, 4], 2, token="t2", total_count=-1),
        _page([5], 3, total_count=-1),
    ])
    items = list(make_client(session).iter_membership_items("pause-ia", page_size=2))
    assert [item["id"] for item in items] == [1, 2, 3, 4, 5]


def test_progression_meme_si_le_serveur_ignore_le_continuation_token():
    """Régression : ne jamais dépendre du seul token pour avancer.

    Un serveur qui renvoie un token mais pagine sur `pageIndex` faisait
    relire la première page en boucle, jusqu'à répétition du token.
    """
    pages = {1: [1, 2], 2: [3, 4], 3: [5]}

    class TokenIgnorant:
        def __init__(self):
            self.index_demandes = []

        def post(self, url, **kwargs):
            return TOKEN_OK

        def get(self, url, params=None, **kwargs):
            index = int((params or {}).get("pageIndex", 1))
            self.index_demandes.append(index)
            return _page(pages.get(index, []), index, token=f"tok{index}")

    session = TokenIgnorant()
    items = list(make_client(session).iter_membership_items("pause-ia", page_size=2))
    assert [item["id"] for item in items] == [1, 2, 3, 4, 5]
    assert session.index_demandes == [1, 2, 3]
