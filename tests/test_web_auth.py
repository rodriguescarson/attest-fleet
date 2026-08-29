"""The operator boundary: reads open, mutations gated, denials audited."""

import importlib
import os

import pytest
from fastapi.testclient import TestClient

from attest_fleet.store import MemoryStore, seed, use_store

TOKEN = "test-operator-token"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("ATTEST_OPERATOR_TOKEN", TOKEN)
    import attest_fleet.web as web
    importlib.reload(web)
    s = MemoryStore()
    seed(s)
    use_store(s)
    with TestClient(web.app) as c:
        yield c, s


def test_reads_are_open(client):
    c, _ = client
    assert c.get("/health").status_code == 200
    assert c.get("/metrics").status_code == 200
    assert c.get("/runs").status_code == 200
    assert c.get("/fleet/identities").status_code == 200


def test_kill_switch_rejects_anonymous_caller(client):
    c, store = client
    r = c.post("/fleet/kill")
    assert r.status_code == 403
    assert store.get_setting("kill_switch") in (None, False)


def test_kill_switch_accepts_the_operator(client):
    c, store = client
    assert c.post("/fleet/kill", headers={"X-Attest-Token": TOKEN}).status_code == 200
    assert store.get_setting("kill_switch") is True
    assert c.post("/fleet/resume", params={"token": TOKEN}).status_code == 200
    assert store.get_setting("kill_switch") is False


def test_approval_cannot_be_granted_anonymously(client):
    c, _ = client
    assert c.post("/approvals/any-id/approve").status_code == 403
    assert c.post("/approvals/any-id/approve/rerun").status_code == 403
    assert c.post("/tickets", json={"subject": "x", "body": "y"}).status_code == 403


def test_denial_is_written_to_the_evidence_trail(client):
    c, store = client
    c.post("/fleet/kill")
    assert any(e["name"] == "auth_denied" for e in store.list("events", limit=100))


def test_a_wrong_token_is_still_rejected(client):
    c, _ = client
    assert c.post("/fleet/kill", headers={"X-Attest-Token": "nope"}).status_code == 403


def test_the_published_token_cannot_reach_the_dangerous_endpoints(monkeypatch):
    """The operator token is published so a reviewer can drive the console, which means it
    is not a secret. Fault injection can turn the whole live board red and the batch
    trigger is unmetered model spend, so neither belongs in what a published string opens."""
    monkeypatch.setenv("ATTEST_OPERATOR_TOKEN", TOKEN)
    monkeypatch.setenv("ATTEST_ADMIN_TOKEN", "admin-secret")
    import attest_fleet.web as web
    importlib.reload(web)
    s = MemoryStore(); seed(s); use_store(s)
    with TestClient(web.app) as c:
        assert c.post("/fleet/fault", params={"rate": 1.0, "token": TOKEN}).status_code == 403
        assert c.post("/tickets/batch", json={"tickets": []},
                      headers={"X-Attest-Token": TOKEN}).status_code == 403
        # the admin token does open them
        assert c.post("/fleet/fault", params={"rate": 0},
                      headers={"x-attest-admin-token": "admin-secret"}).status_code == 200
        # and the published token still drives what a reviewer needs
        assert c.post("/fleet/kill", headers={"X-Attest-Token": TOKEN}).status_code == 200


def test_dangerous_endpoints_are_not_advertised(monkeypatch):
    """They are not secret by obscurity, but they should not be in the public schema a
    reviewer is handed alongside a published credential."""
    monkeypatch.setenv("ATTEST_OPERATOR_TOKEN", TOKEN)
    import attest_fleet.web as web
    importlib.reload(web)
    with TestClient(web.app) as c:
        paths = c.get("/openapi.json").json()["paths"]
        assert "/fleet/fault" not in paths and "/tickets/batch" not in paths
        assert "/tickets" in paths  # what a reviewer does need stays documented


def test_a_malformed_ticket_body_is_a_client_error(client):
    """A 500 on the first endpoint a reviewer touches reads as a broken service."""
    c, _ = client
    r = c.post("/tickets", content=b"not json", headers={"X-Attest-Token": TOKEN,
                                                         "content-type": "application/json"})
    assert r.status_code == 400
    r2 = c.post("/tickets", json={"nope": 1}, headers={"X-Attest-Token": TOKEN})
    assert r2.status_code == 400
