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
