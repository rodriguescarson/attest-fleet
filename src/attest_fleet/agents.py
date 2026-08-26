"""The fleet's agents, built with Google ADK on Gemini 3.5.

Identity list (also served at /fleet/identities):

  fleet_controller  gemini-3.7-flash       decomposes a ticket into typed tasks; read-only tools
  billing_agent     gemini-3.7-flash  refunds and order questions; mutating tools behind policy
  account_agent     gemini-3.7-flash  address, subscription, lock and deletion; same policy
  auditor           gemma-4-31b-it         verifies tasks with no deterministic post-condition

Workers never talk to each other and never see the whole ticket history: the
controller passes exactly the context each task needs. That decoupling is what
makes a per-task claim verifiable."""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext

from . import config, experience, policy, tools
from .domain import AuditVerdict, Claim, Plan
from .store import get_store

AGENT_IDENTITIES = [
    {"name": "fleet_controller", "model": config.CONTROLLER_MODEL, "role": "Task decomposition and customer resolution", "tools": [t.__name__ for t in tools.READ_TOOLS], "mutates": False, "collaborates_with": ["billing_agent", "account_agent"]},
    {"name": "billing_agent", "model": config.WORKER_MODEL, "role": "Refunds and order actions", "tools": [t.__name__ for t in tools.BILLING_TOOLS], "mutates": True, "collaborates_with": ["fleet_controller"]},
    {"name": "account_agent", "model": config.WORKER_MODEL, "role": "Address, subscription, lock and deletion actions", "tools": [t.__name__ for t in tools.ACCOUNT_TOOLS], "mutates": True, "collaborates_with": ["fleet_controller"]},
    {"name": "vision_reader", "model": config.VISION_MODEL, "role": "Reads an image attached to a ticket (screenshot, receipt, photo) into text before planning", "tools": [], "mutates": False, "collaborates_with": ["fleet_controller"]},
    {"name": "auditor", "model": config.AUDITOR_MODEL, "role": "LLM verification for tasks without a deterministic post-condition", "tools": [], "mutates": False, "collaborates_with": []},
]

CONTROLLER_INSTRUCTION = """You are the controller of a customer-operations agent fleet.
You receive one support ticket. Resolve who the customer is, decide what has to happen,
and split it into typed tasks for the specialist workers. You never mutate anything.

Rules:
- Use find_customer first. If it returns more than one match, disambiguate using email,
  order ids, addresses or city mentioned in the ticket. If you still cannot tell, emit ONE
  task of type "other" that explains the ambiguity, and set customer_id to null.
- Task types: refund (billing_agent), address_change / cancel_subscription / unlock_account
  (account_agent), other (account_agent). Fill order_id / subscription_id / amount /
  new_address whenever the ticket and the lookups give them; look them up with
  list_orders / get_subscription instead of guessing.
- Refund amounts: if the customer names an amount use it; otherwise the order total.
- Each task instruction must be self-contained: the worker sees only the instruction,
  never the ticket.
- Be conservative. A wrong action on a real account is worse than an unresolved ticket."""


def _worker_instruction(name: str, base: str):
    def provider(ctx: ReadonlyContext) -> str:
        lessons = experience.lessons_for(get_store(), name)
        text = base
        if lessons:
            text += "\n\nLessons from verified failures of previous runs (follow them):\n" + "\n".join(f"- {l}" for l in lessons)
        return text

    return provider


WORKER_COMMON = """You execute exactly one task, given as JSON in the user message, using your tools.
Then you report a Claim.

Honesty rules — the verifier reads the real system of record after you finish:
- outcome "done" means the post-condition holds in the system of record right now. A tool
  saying "success" is not enough; read the state fields (state, status, locked, address).
- If a tool returns status "pending_approval" or "blocked", the action did NOT happen.
  Report outcome "blocked" and include the approval id in the note.
- If a tool errors or the state is not what the task needs, report "failed".
- confidence is your probability that the outcome is actually true. Be calibrated: a
  confirmed read-back deserves 0.95+, an unconfirmed success message deserves less.
- Never call a mutating tool for a different customer, order or subscription than the task names.
- Do not invent ids. Do not retry a blocked action."""

BILLING_INSTRUCTION = "You are billing_agent. You handle refunds and order questions.\n" + WORKER_COMMON + """
Refund procedure: get_order → check refundable balance → issue_refund → confirm the returned
state is "completed" → record_note on the customer."""

ACCOUNT_INSTRUCTION = "You are account_agent. You handle address changes, subscription cancellations, account unlocks and deletions.\n" + WORKER_COMMON + """
After any change, read the record back (get_customer / get_subscription) and quote the field
you checked in evidence."""


def build_controller() -> LlmAgent:
    return LlmAgent(
        name="fleet_controller",
        model=config.CONTROLLER_MODEL,
        description="Decomposes a support ticket into verifiable tasks.",
        instruction=CONTROLLER_INSTRUCTION,
        tools=list(tools.READ_TOOLS),
        output_schema=Plan,
        output_key="plan",
        mode="task",
    )


def build_worker(name: str) -> LlmAgent:
    if name == "billing_agent":
        instr, tls = BILLING_INSTRUCTION, tools.BILLING_TOOLS
    elif name == "account_agent":
        instr, tls = ACCOUNT_INSTRUCTION, tools.ACCOUNT_TOOLS
    else:
        raise ValueError(name)
    return LlmAgent(
        name=name,
        model=config.WORKER_MODEL,
        description=f"{name} specialist worker",
        instruction=_worker_instruction(name, instr),
        tools=list(tls),
        output_schema=Claim,
        output_key="claim",
        mode="task",
        before_tool_callback=policy.before_tool,
        after_tool_callback=policy.after_tool,
    )


def build_auditor() -> LlmAgent:
    return LlmAgent(
        name="auditor",
        model=config.AUDITOR_MODEL,
        description="Verifies a task outcome from evidence when no deterministic check exists.",
        instruction="""You are an independent auditor. You receive a task, the worker's claim and the raw
tool events. Decide whether the claim's outcome is supported by the evidence. Be skeptical:
a 'success' message without a read-back of the changed field is weak evidence.""",
        output_schema=AuditVerdict,
        output_key="verdict",
        mode="task",
    )


def build_chat_fleet() -> LlmAgent:
    """A chat-mode version for `adk web`: controller with the workers as sub-agents.
    Production uses the explicit orchestrator in fleet.py so every claim is verifiable."""
    billing = build_worker("billing_agent")
    account = build_worker("account_agent")
    for w in (billing, account):
        w.output_schema = None
        w.mode = "chat"
    return LlmAgent(
        name="fleet_controller",
        model=config.CONTROLLER_MODEL,
        description="Customer-operations fleet (interactive demo).",
        instruction=CONTROLLER_INSTRUCTION + "\n\nIn this interactive mode, delegate each task to the matching sub-agent and summarise what they report.",
        tools=list(tools.READ_TOOLS),
        sub_agents=[billing, account],
    )
