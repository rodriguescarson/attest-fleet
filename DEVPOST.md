# Attest Fleet

**Tagline:** An agent fleet that measures its own silent failures, and publishes the number.

---

## Inspiration

An agent reports "refund issued." The payment gateway left it pending. The run counts as a
success, the board stays green, and the customer never gets their money.

Nobody sees that. Not the agent, not the dashboard, not the person on call at 2am. The agent
isn't lying either. It read a tool response that said success and believed it. It genuinely
can't tell.

I kept looking for the number. How often does an enterprise agent fleet say "done" about
something that didn't happen? Every framework I checked computes its success rate from the
agent's own self-report, which means when the agent is wrong about itself, the dashboard is
wrong with it. So I built the thing that measures it.

## What it does

Attest Fleet runs five governed agents on a customer support workload: a controller that
splits a ticket into typed tasks, a billing agent, an account agent, a vision reader for
attachments, and a Gemma auditor. Every worker sees only its own task, never the whole
ticket. That isolation is the point. It's what makes each claim checkable on its own.

Then every claim gets checked against Firestore, the actual system of record. Deterministic
post-conditions first, the LLM auditor only where no post-condition exists. If the agent
said "done" and the record disagrees, that's a silent failure, and it gets counted.

Across 40 tickets with a 30% injected fault rate:

- reported success 53.5%, verified success 51.2%
- silent-failure rate 4.3%, which is 1 of 23 done-claims, 95% CI 0.008 to 0.210
- false alarms 0%
- Brier 0.0214, ECE 0.0307

That interval is wide because it's one event. I'd rather publish the interval than a clean
number that reads more certain than it is.

The rest is the governance a fleet like this needs to be allowed near real money. A policy
gate stops an over-limit refund before it runs and routes it to a human, and the limit is
cumulative per order so five refunds of 98 can't walk through a 100 limit. Model Armor
screens ticket text before any agent reads a word. Agent cards are published to Google's
Agent Registry and read back live, each carrying a contract derived from its real tool
bindings, so widening an agent's authority fails the build. The evidence trail is
hash-chained, tamper-evident rather than tamper-proof, and the README says exactly why the
difference matters.

There's also a second check that I haven't seen elsewhere. Post-conditions tell you the end
state is right. They don't tell you it was reached legitimately. So the route gets checked
too: did the worker read the record back after writing, did every write name the customer
the ticket named. That's reported separately from the verdict, because an end-state pass
with a process failure isn't a silent failure, and calling it one would be its own kind of
wrong.

## How I built it

Google ADK 2.7 for the agents. Gemini 3.7 Flash on Vertex AI for the controller and
workers, Gemma 4 for the auditor, Cloud Run for the service, Firestore as the system of
record, Model Armor on the input boundary, Agent Registry for identity, OpenTelemetry into
Cloud Trace.

The model routing ended up hybrid, and not by choice. Gemini goes to Vertex, and `global`
is the only location serving 3.7 right now. Gemma isn't a Vertex publisher model, so the
auditor keeps the Developer API key. Text-to-speech went the same way, because Vertex
rejects the AUDIO response modality outright. Two clients, one fleet.

The evaluation harness is the part I care about. 40 tickets, faults injected at a known
rate, hidden ground truth on the ones that carry it, and metrics computed over a single
consistent denominator so the reported-versus-verified gap is like for like.

## Challenges I ran into

The calibration was wrong and it took me too long to see it. I was scoring the agent's
stated confidence as if it were P(verified = true). It isn't. It's the agent's confidence in
its own claim, so when a worker says "blocked" at 0.9 it is confidently claiming failure.
Fixing that one line moved Brier from 0.3806 to 0.1806. The bug made my agents look badly
calibrated when the harness was the thing that was broken.

A retry double-spent a refund. 40 went out, the run retried, another 40 went out, and both
verified clean because the end state looked right each time. I added an idempotency guard
and a cumulative threshold. That one bothered me, because it's exactly the failure the
project exists to catch and my own system produced it.

The interpretability probe I'd built didn't survive review. It looked like a strong result,
a linear probe recovering the label from activations at 1.00 AUROC while surface text sat at
chance. Then I read my own generator and found it drew confidence from the same distribution
for both labels. The controls were uninformative by construction. The contrast I was proud of
was an artifact of how I made the data. I retracted it in the README, the results file, and
the dashboard, and moved it to a roadmap section where it belongs.

Smaller ones: Firestore's 1 MiB document limit versus inline audio, an admin token that made
it into a public repo and had to be rotated, and the default compute service account holding
`roles/editor` on a shared project, which is now a dedicated one.

## Accomplishments I'm proud of

The number exists and it's checkable. `/metrics` is live, computed from real runs, and every
figure in the README, the architecture diagram and the demo comes from it. If a judge
disagrees with my arithmetic they can hit the endpoint.

I also measure the checker. On those 40 runs the verifier disagreed with hidden ground truth
twice, both on a ticket where two customers share a name. The write landed, the post-condition
passed, and nothing confirmed it was the right person. A verification layer that hides its own
error rate is asking for exactly the trust it tells you not to give an agent, so that goes in
the README too.

## What I learned

Never trust a declared shape. Read it from the file. Half the bugs above came from believing
a number that looked right instead of checking where it came from, and I did that in a project
whose entire argument is that you shouldn't.

The other thing: a retraction is cheaper than a defense. Pulling the probe result cost me a
good-looking chart. Keeping it would have cost the credibility of everything next to it.

## What's next

Per-agent calibration instead of fleet-wide, so a badly calibrated worker can't hide behind
the average. More verifier coverage on the ambiguous-entity case that produced both blind
spots. And running the sweep at a few thousand tickets, because a rate over 23 claims has an
interval wide enough to drive a truck through, and I'd like to narrow it.

## Built with

Google ADK 2.7, Gemini 3.7 Flash, Gemma 4, Vertex AI, Google Agent Registry (A2A), Model
Armor, Cloud Run, Firestore, OpenTelemetry, Cloud Trace, Python, FastAPI.
