"""Agent Registry and Model Armor integration: fallback behaviour without cloud access."""

from attest_fleet import guard, registry
from attest_fleet.agents import AGENT_IDENTITIES


def test_identities_fall_back_to_the_local_list_without_cloud(monkeypatch):
    """The registry is an enrichment, never a hard dependency: no credentials must still
    yield the full fleet, marked as locally sourced."""
    monkeypatch.setattr(registry, "_cache", {"at": 0.0, "agents": None, "source": "local"})
    monkeypatch.setattr(registry, "_fetch", lambda: None)
    agents, source = registry.registered_agents()
    assert source == "local"
    assert [a["name"] for a in agents] == [a["name"] for a in AGENT_IDENTITIES]
    assert all(a["registered"] is False for a in agents)


def test_identities_are_enriched_when_the_registry_answers(monkeypatch):
    monkeypatch.setattr(registry, "_cache", {"at": 0.0, "agents": None, "source": "local"})
    monkeypatch.setattr(registry, "_fetch", lambda: [{
        "name": "projects/p/locations/global/agents/agentregistry-abc",
        "displayName": "billing_agent",
        "agentId": "urn:agent:test",
        "version": "1.0.0",
        "skills": [{"id": "issue_refund"}, {"id": "get_order"}],
        "protocols": [{"type": "A2A_AGENT"}],
    }])
    agents, source = registry.registered_agents()
    assert source == "agent-registry"
    billing = next(a for a in agents if a["name"] == "billing_agent")
    assert billing["registered"] is True
    assert billing["registry"]["skills"] == ["issue_refund", "get_order"]
    # agents absent from the registry are still listed, just unmarked
    assert next(a for a in agents if a["name"] == "auditor")["registered"] is False


def test_a_registry_outage_does_not_break_identities(monkeypatch):
    monkeypatch.setattr(registry, "_cache", {"at": 0.0, "agents": None, "source": "local"})

    def boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr(registry, "_fetch", boom)
    agents, source = registry.registered_agents()
    assert source == "local" and len(agents) == len(AGENT_IDENTITIES)


def test_model_armor_is_skipped_when_unconfigured(monkeypatch):
    monkeypatch.setattr(guard.config, "MODEL_ARMOR_TEMPLATE", "")
    v = guard.screen("anything")
    assert v["checked"] is False and v["blocked"] is False


def test_model_armor_fails_open_not_closed(monkeypatch):
    """If the guard is unreachable the ticket still runs: the verifier, the state gate and
    the approval gate all still apply, and dropping real tickets is the worse failure."""
    monkeypatch.setattr(guard.config, "MODEL_ARMOR_TEMPLATE", "t")
    monkeypatch.setattr(guard.config, "VERTEX_PROJECT", "p")
    monkeypatch.setattr(guard, "_endpoint", lambda: "https://invalid.invalid/x")
    v = guard.screen("refund my order")
    assert v["blocked"] is False and v["checked"] is False
    assert "unavailable" in v["reason"]


def test_empty_text_is_not_screened(monkeypatch):
    monkeypatch.setattr(guard.config, "MODEL_ARMOR_TEMPLATE", "t")
    monkeypatch.setattr(guard.config, "VERTEX_PROJECT", "p")
    assert guard.screen("   ")["checked"] is False


def test_registry_matches_either_display_name_form(monkeypatch):
    """The registry labels a derived Agent with the card's `name` ("billing_agent"), while
    the Service we create is labelled "Attest Fleet · billing_agent". Both must match, so a
    label change on either side cannot silently mark the whole fleet unregistered."""
    for label in ("billing_agent", "Attest Fleet · billing_agent"):
        monkeypatch.setattr(registry, "_cache", {"at": 0.0, "agents": None, "source": "local"})
        monkeypatch.setattr(registry, "_fetch", lambda label=label: [
            {"name": "projects/p/locations/global/agents/x", "displayName": label,
             "agentId": "urn:agent:test", "version": "1.0.0",
             "skills": [{"id": "issue_refund"}], "protocols": [{"type": "A2A_AGENT"}]}
        ])
        agents, source = registry.registered_agents()
        assert source == "agent-registry", label
        assert next(a for a in agents if a["name"] == "billing_agent")["registered"] is True, label


def test_fabricated_runs_cannot_reach_a_deployed_store():
    """The board is something a judge may read as live results, so sample data must not be
    able to reach a real store — not even behind a force flag. This test exists because it
    once could: /admin/seed forced fabricated runs into the production Firestore board."""
    from attest_fleet import demo

    class FakeFirestore(dict):
        backend = "firestore"
        def set(self, col, doc_id, doc): raise AssertionError("fabricated run written to a deployed store")
        def get(self, col, doc_id): return None

    demo.seed_demo(FakeFirestore())            # must be a no-op
    demo.seed_demo(FakeFirestore(), force=True)  # even forced


def test_agent_contracts_match_the_live_tool_bindings():
    """A contract that can silently stop being true is worth less than no contract. This
    re-derives every field from what ADK actually binds, so handing billing_agent an
    address tool fails the build rather than quietly making the published card wrong."""
    from attest_fleet import contracts
    contracts.verify_contracts()


def test_worker_write_scopes_are_disjoint():
    """The multi-agent split exists to create separate least-privilege write scopes. If the
    workers can reach each other's mutations, the split has stopped paying for itself."""
    from attest_fleet.contracts import contract_for
    billing = set(contract_for("billing_agent")["may_mutate"])
    account = set(contract_for("account_agent")["may_mutate"])
    assert "issue_refund" in billing and "issue_refund" not in account
    assert "update_address" in account and "update_address" not in billing
    assert "delete_account" not in billing


def test_the_agent_with_the_most_context_has_no_authority():
    """fleet_controller is the only component that sees the whole ticket, so it holds no
    mutating tool at all."""
    from attest_fleet.contracts import contract_for
    assert contract_for("fleet_controller")["may_mutate"] == []
    assert contract_for("vision_reader")["may_call"] == []


def test_both_stores_agree_on_list_ordering():
    """A listing that means one thing in tests and another in production is worse than no
    listing. MemoryStore returns oldest-first within its window; Firestore must match."""
    from attest_fleet.store import FirestoreStore, MemoryStore
    assert set(FirestoreStore._ORDER_BY) == {"runs", "events", "approvals"}
    s = MemoryStore()
    for i in range(5):
        s.set("runs", f"r{i}", {"id": f"r{i}", "started_at": f"2026-08-2{i}T00:00:00Z"})
    got = [r["id"] for r in s.list("runs", limit=3)]
    assert got == ["r2", "r3", "r4"]  # the newest three, oldest-first within the window
