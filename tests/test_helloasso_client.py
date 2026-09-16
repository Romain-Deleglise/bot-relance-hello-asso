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


def test_pagination_par_continuation_token():
    page1 = FakeResponse(payload={
        "data": [{"id": 1}, {"id": 2}],
        "pagination": {"pageIndex": 1, "totalPages": 2, "continuationToken": "abc"},
    })
    page2 = FakeResponse(payload={
        "data": [{"id": 3}],
        "pagination": {"pageIndex": 2, "totalPages": 2, "continuationToken": None},
    })
    session = FakeSession([TOKEN_OK], [page1, page2])
    items = list(make_client(session).iter_membership_items("pause-ia"))

    assert [item["id"] for item in items] == [1, 2, 3]
    assert session.get_calls[0][1]["tierTypes"] == ["Membership"]
    assert session.get_calls[0][1]["itemStates"] == ["Processed", "Registered"]
    assert session.get_calls[1][1]["continuationToken"] == "abc"
    assert session.get_calls[0][0].endswith("/organizations/pause-ia/items")


def test_401_en_cours_de_route_declenche_une_reauthentification():
    session = FakeSession(
        [TOKEN_OK, TOKEN_OK],
        [
            FakeResponse(401, text="expired"),
            FakeResponse(payload={"data": [{"id": 1}],
                                  "pagination": {"pageIndex": 1, "totalPages": 1}}),
        ],
    )
    items = list(make_client(session).iter_membership_items("pause-ia"))
    assert [item["id"] for item in items] == [1]
    assert session.token_responses == []  # deux authentifications consommées
