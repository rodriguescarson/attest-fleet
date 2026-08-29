"""A tamper-evident chain over the evidence trail.

This project's argument is that a self-report is not evidence. Attest's own evidence trail
was, until this module, exactly that: a set of rows anyone with write access could edit
afterwards, with nothing to show it had happened. A verification layer whose audit log can
be quietly rewritten has the same hole it was built to close, one level up.

So each event carries the hash of the event before it in the same run. Editing a record,
deleting one, or reordering them breaks every link from that point on, and `verify_chain`
says exactly where.

WHAT THIS IS NOT. This is tamper-EVIDENT, not tamper-PROOF, and the difference matters:

- The service writes the chain, so a compromised service, or anyone holding the runtime's
  credentials, can rewrite a record and recompute every hash after it. Detecting that needs
  an anchor outside this trust boundary, which this does not have.
- It is not a blockchain, there is no consensus, and no external timestamping authority.
- The guarantee is narrow and real: a record altered in the datastore without recomputing
  the whole forward chain is detectable, and the evidence package proves the chain was
  intact at the moment it was generated.

Stating that plainly is the point. A verification system that overclaims its own guarantees
is arguing against itself.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

GENESIS = "0" * 64

# Fields that are part of what the event asserts. `prev_hash` and `hash` are excluded
# because they describe the chain rather than the evidence.
_SIGNED_FIELDS = ("id", "run_id", "task_id", "agent", "kind", "name",
                  "args_json", "result_json", "latency_ms", "ts", "seq")


def digest(event: dict[str, Any], prev_hash: str) -> str:
    """The hash binding one event to its predecessor."""
    payload = json.dumps({k: event.get(k) for k in _SIGNED_FIELDS},
                         sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{prev_hash}\n{payload}".encode()).hexdigest()


def _ordered(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chained events in chain order. Sequence first: several events can share a timestamp."""
    return sorted([e for e in events if e.get("hash")], key=lambda e: e.get("seq") or 0)


def head(store, run_id: str) -> tuple[str, int]:
    """The hash and sequence of the newest event in this run, or GENESIS at position 0.

    Reads rather than caching in memory on purpose: Cloud Run scales to more than one
    instance, and a chain head held in a process would fork the moment it did."""
    chained = _ordered(store.query("events", run_id=run_id))
    if not chained:
        return GENESIS, 0
    last = chained[-1]
    return last["hash"], (last.get("seq") or 0) + 1


def link(store, event: dict[str, Any]) -> dict[str, Any]:
    """Attach prev_hash and hash to an event before it is written."""
    prev, seq = head(store, event.get("run_id", ""))
    event["prev_hash"] = prev
    event["seq"] = seq
    event["hash"] = digest(event, prev)
    return event


def verify_chain(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Recompute the chain over one run's events, oldest first.

    Returns the verdict plus the first event whose link does not hold, so a break is
    located rather than merely announced."""
    chained = _ordered(events)
    unchained = [e for e in events if not e.get("hash")]
    if not chained:
        return {"intact": None, "checked": 0, "unchained": len(unchained),
                "detail": "no chained events in this run; nothing here is attested"}

    prev = GENESIS
    for i, e in enumerate(chained):
        if e.get("prev_hash") != prev:
            return {"intact": False, "checked": i, "broken_at": e.get("id"),
                    "detail": f"event {e.get('id')} does not follow the previous one; "
                              "a record was edited, removed or reordered"}
        if digest(e, prev) != e.get("hash"):
            return {"intact": False, "checked": i, "broken_at": e.get("id"),
                    "detail": f"event {e.get('id')} does not match its own hash; its contents changed"}
        prev = e["hash"]

    # An unchained event is rendered in the package as evidence but cannot be attested.
    # Filtering it out and still returning "intact" would be a verification system
    # reporting a clean result over data it declined to check, which is the precise
    # failure this project exists to catch.
    if unchained:
        return {"intact": False, "checked": len(chained), "unchained": len(unchained),
                "head": prev,
                "unchained_ids": [e.get("id") for e in unchained][:10],
                "detail": f"{len(chained)} events link correctly, but {len(unchained)} carry "
                          "no hash and are therefore unattested; the trail is not fully covered"}

    return {"intact": True, "checked": len(chained), "unchained": 0, "head": prev,
            "detail": "every event links to the one before it",
            "scope": "tamper-evident against record edits; the service writes the chain, "
                     "so it is not proof against a compromised writer"}
