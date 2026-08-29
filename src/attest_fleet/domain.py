"""Typed contracts between the fleet's agents, the verifier and the evidence store.

Two of these are LLM output schemas (Plan, Claim), so they use only explicit,
JSON-schema-friendly fields — no free-form dicts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


TaskType = Literal["refund", "address_change", "cancel_subscription", "unlock_account", "delete_account", "other"]
WorkerName = Literal["billing_agent", "account_agent"]
Outcome = Literal["done", "failed", "blocked"]


class Ticket(BaseModel):
    """The enterprise trigger. Comes from a webhook, a mailbox or Pub/Sub — never a chat box."""

    id: str = Field(default_factory=lambda: new_id("tkt"))
    source: str = "webhook"
    customer_ref: str = Field(description="Whatever the customer gave us: an id, a name, an email.")
    subject: str
    body: str
    created_at: str = Field(default_factory=now_iso)
    image_url: Optional[str] = Field(default=None, description="Optional attachment (https or data: URL) the vision reader describes before planning.")
    audio_url: Optional[str] = Field(default=None, description="Optional call recording or voicemail (https or data: URL) transcribed before planning.")
    # Ground truth for the eval harness only. Stripped before any agent sees the ticket.
    expected: Optional["Expectation"] = None


class Expectation(BaseModel):
    task_type: TaskType
    customer_id: Optional[str] = None
    order_id: Optional[str] = None
    subscription_id: Optional[str] = None
    amount: Optional[float] = None
    new_address: Optional[str] = None
    should_block: bool = False  # policy should stop this one (needs approval)
    trap: Optional[str] = None  # what the harness planted, for the write-up


class Task(BaseModel):
    """One unit of work the controller hands to a specialist."""

    id: str = Field(description="Short stable id like t1, t2.")
    type: TaskType
    worker: WorkerName
    customer_id: Optional[str] = Field(default=None, description="Resolved customer id, if known.")
    order_id: Optional[str] = None
    subscription_id: Optional[str] = None
    amount: Optional[float] = None
    new_address: Optional[str] = None
    instruction: str = Field(description="Precise, self-contained instruction for the worker.")
    rationale: str = Field(description="Why this task, in one sentence.")


class Plan(BaseModel):
    """Controller output: the decomposition of a ticket."""

    tasks: list[Task]
    customer_resolution: str = Field(
        description="How the customer reference was resolved, including any ambiguity found."
    )
    summary: str


class Claim(BaseModel):
    """Worker output. This is what the agent *says* happened. Attest checks it."""

    task_id: str
    outcome: Outcome = Field(description="done = post-conditions met; failed = could not; blocked = policy/approval stopped it.")
    confidence: float = Field(ge=0, le=1, description="Probability that the outcome is actually true in the system of record.")
    actions: list[str] = Field(default_factory=list, description="Tools called, in order, with the key arguments.")
    evidence: str = Field(description="What the tool results showed. Quote states and ids.")
    note: str = Field(default="", description="Anything a human reviewer should know.")


class Check(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class Verification(BaseModel):
    task_id: str
    verified: Optional[bool]  # None = could not verify
    method: Literal["postcondition", "auditor", "none"]
    checks: list[Check] = Field(default_factory=list)
    silent_failure: bool = False  # claimed done, world disagrees
    false_alarm: bool = False  # claimed failed/blocked, world says it is fine
    detail: str = ""
    # Process conformance, kept SEPARATE from `verified` on purpose. An end-state pass with
    # a process failure is not a silent failure, and folding the two together would lose
    # exactly the distinction this field exists to make.
    process_checks: list[Check] = Field(default_factory=list)
    process_conformant: Optional[bool] = None


class AuditVerdict(BaseModel):
    """Auditor LLM output for tasks with no deterministic post-condition."""

    verified: bool
    reasoning: str


class Approval(BaseModel):
    id: str = Field(default_factory=lambda: new_id("apr"))
    run_id: str
    ticket_id: str
    task_id: str
    agent: str
    action: str
    args_json: str
    risk_reason: str
    status: Literal["pending", "approved", "rejected"] = "pending"
    created_at: str = Field(default_factory=now_iso)
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None


class Event(BaseModel):
    """One row of execution evidence."""

    id: str = Field(default_factory=lambda: new_id("evt"))
    run_id: str
    task_id: Optional[str] = None
    agent: Optional[str] = None
    kind: Literal["model", "tool", "policy", "verify", "run", "experience"]
    name: str
    args_json: str = ""
    result_json: str = ""
    latency_ms: Optional[int] = None
    ts: str = Field(default_factory=now_iso)
    # Chain links, set by chain.link() at write time. See chain.py for what they do and,
    # more importantly, what they do not guarantee.
    prev_hash: Optional[str] = None
    hash: Optional[str] = None
    # Position within this run's chain. Timestamps collide when several events are written
    # in the same millisecond, so order is explicit rather than inferred from `ts`.
    seq: Optional[int] = None


class TaskResult(BaseModel):
    task: Task
    claim: Optional[Claim]
    verification: Optional[Verification]


class RunRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    ticket: Ticket
    plan: Optional[Plan] = None
    results: list[TaskResult] = Field(default_factory=list)
    status: Literal["running", "verified", "silent_failure", "unverified", "pending_approval", "failed", "killed"] = "running"
    started_at: str = Field(default_factory=now_iso)
    finished_at: Optional[str] = None
    error: Optional[str] = None
    vision: Optional[str] = None  # what the vision reader saw in an attached image
    voice: Optional[str] = None   # what the voice reader heard in an attached call recording
    ground_truth: Optional[bool] = None  # eval harness: did the world end up as expected?


Ticket.model_rebuild()
