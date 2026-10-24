This agent reported it cancelled a customer's subscription. Ninety-five percent confident. It didn't -- the cancel only got as far as 'requested'. The agent isn't lying. It genuinely can't tell. That's a silent failure.


Every agent framework computes its success rate from the agent's own self-report. When the agent is wrong about itself, the dashboard is wrong with it. So we measured it. Across forty tickets with faults injected: agents reported fifty-three percent success, fifty-one percent actually held up in the record. Silent-failure rate, four point three percent -- one of twenty-three 'done' claims that wasn't true.


That's one event, so the ninety-five percent interval runs from under one percent to twenty-one. I'm telling you that because this whole project is an argument against numbers taken on trust.


A customer leaves a voicemail. Gemini transcribes it, and it contains two different asks. The controller splits them -- refund to the billing agent, address change to the account agent. Each worker sees only its own task, never the whole ticket, and that isolation is what makes each claim independently checkable. Both verified. Not because the agents said so. Because the records say so.


Gemini 3.7 Flash on Vertex AI, running on Cloud Run, Firestore as the system of record.


These agents move real money, so a deterministic gate stops an over-limit refund before it runs. Every entry here has one of those. The difference is that a block is a counted outcome -- the worker reports blocked, not done, and it never lands in the success rate.


And the boundary is real. No token, four-oh-three, logged as evidence. The credential in our README is tiered -- it drives what a reviewer needs, and cannot reach fault injection.


Ticket text is untrusted input. Google's Model Armor screens it before any agent reads a word. Blocked, high confidence, zero tasks planned. The controller never saw that sentence.


Post-conditions tell you the end state is right, not that it was reached legitimately. So we check the route too. Here's a task that passed the end-state check and failed the process check -- the refund landed, the agent never read it back. Reported separately, because that is not a silent failure.


These aren't a hardcoded list -- they're published to Google's Agent Registry as A2A cards and read back live. Each carries a contract derived from its real tool bindings, so if someone widens an agent's authority, the build fails.


We also measure the checker. On those forty runs the verifier was wrong twice -- both on a ticket where two customers share a name. The write landed, the post-condition passed, and nothing checked it was the right person. We publish that, because a verification layer that hides its own error rate is asking for the trust it tells you not to give an agent.


Most agent systems measure whether an agent said it succeeded. Attest measures whether it actually happened -- and then tells you how much to trust the measurement. Because in production, 'done' isn't a result. It's a claim.
