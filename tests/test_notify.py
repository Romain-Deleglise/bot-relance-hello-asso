"""Tests de la couche d'alerte/supervision (aucun réseau réel)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relance_adhesions import notify  # noqa: E402


class FakeRequests:
    """Enregistre les appels ; peut simuler une panne réseau."""

    def __init__(self, boom=False):
        self.boom = boom
        self.gets = []
        self.posts = []

    def get(self, url, timeout=None):
        self.gets.append(url)
        if self.boom:
            raise notify.requests.RequestException("réseau KO")

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        if self.boom:
            raise notify.requests.RequestException("réseau KO")


def test_ping_healthcheck_succes_et_echec(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(notify, "requests", _wrap(fake))
    notify.ping_healthcheck("https://hc.example/abc", success=True)
    notify.ping_healthcheck("https://hc.example/abc", success=False)
    assert fake.gets == ["https://hc.example/abc", "https://hc.example/abc/fail"]


def test_ping_healthcheck_sans_url_ne_fait_rien(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(notify, "requests", _wrap(fake))
    notify.ping_healthcheck("", success=True)
    assert fake.gets == []


def test_send_alert_porte_les_deux_cles(monkeypatch):
    """content (Discord) ET text (Slack) : le même appel marche pour les deux."""
    fake = FakeRequests()
    monkeypatch.setattr(notify, "requests", _wrap(fake))
    notify.send_alert("https://hooks.example/xyz", "panne")
    assert fake.posts == [("https://hooks.example/xyz", {"content": "panne", "text": "panne"})]


def test_send_alert_avale_les_erreurs_reseau(monkeypatch):
    """Une alerte qui échoue ne doit jamais lever (ne pas casser le run)."""
    fake = FakeRequests(boom=True)
    monkeypatch.setattr(notify, "requests", _wrap(fake))
    notify.send_alert("https://hooks.example/xyz", "panne")  # ne lève pas
    notify.ping_healthcheck("https://hc.example/abc", success=True)  # ne lève pas


def _wrap(fake):
    """Conserve l'exception RequestException réelle sur l'objet simulé."""
    fake.RequestException = notify.requests.RequestException
    return fake
