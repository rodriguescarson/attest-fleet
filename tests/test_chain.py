"""The evidence trail attests to itself: an edited record must be detectable."""

from attest_fleet import chain, policy
from attest_fleet.store import MemoryStore, seed, use_store


def _trail(n=4):
    s = MemoryStore(); seed(s); use_store(s)
    for i in range(n):
        policy.record_event(run_id="r", kind="tool", name=f"step_{i}",
                            args_json=f'{{"i":{i}}}', result_json='{"status":"success"}')
    return s, s.query("events", run_id="r")


def test_an_untouched_trail_verifies():
    _, events = _trail()
    v = chain.verify_chain(events)
    assert v["intact"] is True and v["checked"] == 4


def test_editing_a_record_breaks_the_chain():
    """The point of the whole exercise: a rewritten result must not pass silently."""
    s, _ = _trail()
    target = sorted(s.query("events", run_id="r"), key=lambda e: e["ts"])[1]
    s.update("events", target["id"], {"result_json": '{"status":"success","amount":99999}'})
    v = chain.verify_chain(s.query("events", run_id="r"))
    assert v["intact"] is False
    assert v["broken_at"] == target["id"]
    assert "contents changed" in v["detail"]


def test_deleting_a_record_breaks_the_chain():
    s, _ = _trail()
    ordered = sorted(s.query("events", run_id="r"), key=lambda e: e["ts"])
    remaining = [e for e in ordered if e["id"] != ordered[1]["id"]]
    v = chain.verify_chain(remaining)
    assert v["intact"] is False and "removed" in v["detail"]


def test_first_event_anchors_to_genesis():
    s, _ = _trail(1)
    first = s.query("events", run_id="r")[0]
    assert first["prev_hash"] == chain.GENESIS
    assert first["hash"] == chain.digest(first, chain.GENESIS)


def test_the_verdict_states_what_it_does_not_prove():
    """A verification system that overclaims its own guarantees argues against itself."""
    _, events = _trail()
    v = chain.verify_chain(events)
    assert "not proof against a compromised writer" in v["scope"]


def test_runs_are_chained_independently():
    s, _ = _trail()
    policy.record_event(run_id="other", kind="tool", name="x")
    other = s.query("events", run_id="other")[0]
    assert other["prev_hash"] == chain.GENESIS   # a separate run starts its own chain


def test_unchained_events_are_not_quietly_excluded():
    """Filtering out what cannot be checked and still reporting "intact" would be a
    verification system returning a clean result over data it declined to verify."""
    s, _ = _trail(3)
    s.set("events", "evt_legacy", {"id": "evt_legacy", "run_id": "r", "kind": "experience",
                                   "name": "lesson_captured", "ts": "2026-01-01T00:00:00Z"})
    v = chain.verify_chain(s.query("events", run_id="r"))
    assert v["intact"] is False
    assert v["unchained"] == 1 and "evt_legacy" in v["unchained_ids"]
    assert "unattested" in v["detail"]


def test_an_empty_trail_says_nothing_is_attested():
    v = chain.verify_chain([])
    assert v["intact"] is None and "nothing here is attested" in v["detail"]
