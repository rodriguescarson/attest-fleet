# Architecture — Attest Fleet

## The fleet and the governance layer

```mermaid
flowchart TB
    subgraph trigger [1 · Trigger]
        WH[Webhook / Pub/Sub push] -->|POST /tickets| API[FastAPI on Cloud Run]
    end

    API --> CTRL

    subgraph plane [Agent fleet · Gemini 3.5 + ADK]
        CTRL[fleet_controller<br/>gemini-3.5-flash-lite<br/>2 · decompose + resolve customer]
        CTRL -->|3 · one Task each, isolated context| BILL[billing_agent<br/>refunds, orders]
        CTRL -->|3 · one Task each| ACCT[account_agent<br/>address, cancel, unlock, delete]
    end

    subgraph gate [7 · Policy gate — every mutating tool]
        KILL{kill switch?}
        RISK{high-risk?<br/>refund > limit · delete}
    end

    BILL -->|4 · tool call| KILL
    ACCT -->|4 · tool call| KILL
    KILL -->|engaged| BLOCK[block · worker reports 'blocked']
    KILL -->|off| RISK
    RISK -->|yes, unapproved| APPR[approval doc<br/>worker reports 'blocked']
    RISK -->|no / approved| TOOLS[tools mutate the system of record]

    TOOLS --> FS[(Firestore<br/>customers · orders · subscriptions)]
    TOOLS -->|6 · args, result, latency| EV[(Firestore · events)]

    FS --> VER
    BILL -. Claim: done? confidence .-> VER
    ACCT -. Claim .-> VER

    subgraph attest [5 · Verification — the point]
        VER[verifier<br/>reads post-conditions from the store<br/>NOT the agent's self-report]
        VER --> SF{claim = done<br/>but world disagrees?}
        SF -->|yes| SILENT[silent failure]
        VER -.->|no deterministic check| AUD[auditor<br/>gemini-3.5-flash-lite]
    end

    SILENT --> EXP
    subgraph learn [8 · Experience]
        EXP[playbook lesson<br/>keyed by failure signature] -->|injected into instruction| BILL
        EXP -->|next run| ACCT
    end

    VER --> MET[metrics<br/>reported vs verified · silent-failure rate<br/>Brier / ECE · risk-coverage · escalation threshold]
    MET --> DASH[Operator dashboard /<br/>kill switch · approvals · evidence]
    APPR --> DASH
```

## Why the topology matters for scoring

- **Decoupling (30% Architectural Discipline).** Workers never talk to each other and never see the
  ticket — the controller hands each one an isolated `Task`. That isolation is what makes a per-task
  claim independently verifiable. Skill/tool/state are separate concerns (ADK tools, policy callbacks,
  Firestore), not collapsed into prompt strings.
- **Beyond chat (40% Operational Utility).** The trigger is an enterprise event (webhook/Pub/Sub), the
  fleet executes real mutations against a system of record, and high-risk actions are gated and
  reversible. No chat box anywhere.
- **Provable (30% Demo/Production-Readiness).** Every tool call is captured as evidence; the verifier
  produces a measured silent-failure rate; the dashboard shows it live on Cloud Run.

## Agent identity list

Served live at `GET /fleet/identities`.

| Agent | Model | Capability boundary | Mutates | Collaborates with |
|---|---|---|---|---|
| `fleet_controller` | gemini-3.5-flash-lite | Decompose ticket, resolve customer (read-only tools) | no | billing_agent, account_agent |
| `billing_agent` | gemini-3.5-flash-lite | Refunds, order reads | yes (gated) | fleet_controller |
| `account_agent` | gemini-3.5-flash-lite | Address, cancel, unlock, delete | yes (gated) | fleet_controller |
| `vision_reader` | gemini-3.5-flash-lite | Read an image attached to a ticket (screenshot, receipt, photo) into text | no | fleet_controller |
| `auditor` | **gemma-4-31b-it** | Verify tasks with no deterministic post-condition — a different model family, so it doesn't share the workers' blind spots | no | — |

The controller model is env-configurable (`ATTEST_CONTROLLER_MODEL`); it moves to `gemini-3.5-flash`
once demand-shed 503s clear. The failure-mode table is in the [README](../README.md#failure-mode-table).
