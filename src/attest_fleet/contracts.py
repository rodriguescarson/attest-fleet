"""Agent contracts: what each agent may do, stated explicitly and checked against reality.

An agent whose limits live only in a prompt is not a principal, it is a suggestion. This
module states each agent's authority as data, so it can be published to the Agent Registry,
shown to an operator, and asserted in a test.

The important property is that **nothing here is aspirational**. Every field is derived
from an enforcement point that already exists in the code:

- `may_call` comes from the tool list ADK actually binds to the agent, so a tool that is
  not listed is not merely discouraged, it is absent from the agent's world.
- `may_mutate` is the intersection of that list with `tools.MUTATING`.
- `must_not_call` is computed as every mutating tool in the fleet minus this agent's own,
  so the disjointness between billing and account authority is a derived fact rather than
  a claim someone remembered to keep true.
- `needs_human_above` and the budgets are the live values the policy gate reads.

`verify_contracts()` re-derives all of it and raises on drift, and a test calls it. If
someone hands `billing_agent` an address tool, the contract does not quietly become wrong;
the build fails.
"""

from __future__ import annotations

from typing import Any

from . import config, tools

# Tools every agent is allowed to reach for, and the ones nobody may reach for directly.
_ALL_MUTATING = sorted(tools.MUTATING)

_AGENT_TOOLS: dict[str, list] = {
    "fleet_controller": tools.READ_TOOLS,
    "billing_agent": tools.BILLING_TOOLS,
    "account_agent": tools.ACCOUNT_TOOLS,
    "vision_reader": [],
    "auditor": [],
}

# Why each agent's authority is bounded the way it is. Stated so an operator reading the
# registry sees the reasoning, not just the permission.
_RATIONALE = {
    "fleet_controller": "Sees the whole ticket, so it holds no mutating tool at all: the "
                        "component with the most context has the least authority.",
    "billing_agent": "Moves money. Cannot touch addresses, subscriptions or account state, "
                     "so a confused or injected billing agent has no lever to reach them.",
    "account_agent": "Changes account state. Holds no refund tool, so it cannot move money "
                     "however it is prompted.",
    "vision_reader": "Reads an attached image into text. No tools at all: an attachment is "
                     "attacker-controlled input and must not carry authority.",
    "auditor": "Judges a claim when no deterministic check exists. No tools, and a "
               "different model family from the workers, so it cannot act on its own verdict.",
}


def contract_for(name: str) -> dict[str, Any]:
    """The authority granted to one agent, derived from what the code enforces."""
    granted = sorted(t.__name__ for t in _AGENT_TOOLS.get(name, []))
    mutating = sorted(set(granted) & tools.MUTATING)
    return {
        "agent": name,
        "may_call": granted,
        "may_mutate": mutating,
        "must_not_call": sorted(set(_ALL_MUTATING) - set(mutating)),
        "needs_human_above": (f"cumulative refund of {config.REFUND_APPROVAL_THRESHOLD:.0f} per order"
                              if "issue_refund" in mutating else None),
        "always_needs_human": ["delete_account"] if "delete_account" in mutating else [],
        "max_tool_calls_per_task": config.MAX_TOOL_CALLS_PER_TASK,
        "max_seconds_per_turn": config.AGENT_TURN_TIMEOUT_S,
        "enforced_at": "policy.before_tool",
        "why": _RATIONALE.get(name, ""),
    }


def all_contracts() -> dict[str, dict[str, Any]]:
    return {name: contract_for(name) for name in _AGENT_TOOLS}


def verify_contracts() -> None:
    """Assert the contracts still describe the fleet. Raises on drift.

    A contract that can silently stop being true is worth less than no contract, so this
    re-derives every field from the live tool bindings rather than trusting the text.
    """
    from .agents import AGENT_IDENTITIES

    listed = {a["name"] for a in AGENT_IDENTITIES}
    if listed != set(_AGENT_TOOLS):
        raise AssertionError(f"contract coverage drifted: {listed ^ set(_AGENT_TOOLS)}")

    for identity in AGENT_IDENTITIES:
        name = identity["name"]
        c = contract_for(name)
        if sorted(identity["tools"]) != c["may_call"]:
            raise AssertionError(f"{name}: registry tools {identity['tools']} != contract {c['may_call']}")
        if identity["mutates"] != bool(c["may_mutate"]):
            raise AssertionError(f"{name}: mutates={identity['mutates']} but may_mutate={c['may_mutate']}")
        overlap = set(c["may_mutate"]) & set(c["must_not_call"])
        if overlap:
            raise AssertionError(f"{name}: tool both granted and forbidden: {overlap}")

    # The separation the whole design rests on: no mutating tool is held by two workers.
    billing = set(contract_for("billing_agent")["may_mutate"])
    account = set(contract_for("account_agent")["may_mutate"])
    shared = billing & account
    if shared - {"record_note"}:
        raise AssertionError(f"worker write scopes are not disjoint: {shared}")
