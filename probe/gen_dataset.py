"""Phase 1 — labeled probe dataset, generated deterministically from Attest's real
fault patterns (no LLM/API needed). Each example is an AUDITOR-INPUT text in the exact
format the auditor sees (TASK / CLAIM / TOOL EVENTS). Label 1 = silent failure (worker
claims done, the read-back disagrees), 0 = genuine (read-back confirms).

Both classes carry a 'success' tool message and a read-back event; the ONLY signal is
whether the read-back VALUE matches the request. So a probe must encode the compare,
not a keyword. Balanced across the four task types."""
import json, random, argparse
from pathlib import Path

CUS = [f"cus_{n}" for n in range(1001, 1099)]
ORD = [f"ord_{n}" for n in range(5001, 5099)]
SUB = [f"sub_{n}" for n in range(9001, 9099)]
STREETS = ["MG Road, Bengaluru 560001","Koregaon Park, Pune 411001","Century Ave, Shanghai 200120",
           "Southern Ave, Kolkata 700029","Nanjing Rd, Shanghai 200001","Rua Augusta, Lisboa 1100",
           "Baker St, London NW1","5th Avenue, New York 10001","Marine Drive, Mumbai 400020"]
AMOUNTS = [19.0,29.0,49.0,80.0,120.0,199.0,299.0,490.0]

def evtext(tool, args, result): return {"tool": tool, "args": json.dumps(args), "result": json.dumps(result)}

def refund(rng, silent):
    cid, oid, amt = rng.choice(CUS), rng.choice(ORD), rng.choice(AMOUNTS)
    task = {"id":"t1","type":"refund","worker":"billing_agent","customer_id":cid,"order_id":oid,
            "amount":amt,"instruction":f"Refund {amt} on {oid}."}
    events = [evtext("get_order",{"order_id":oid},{"id":oid,"total":amt,"refunded":0.0,"status":"paid"}),
              evtext("issue_refund",{"order_id":oid,"amount":amt},{"state":"completed","message":"refund submitted"})]
    if silent:  # gateway accepted then dropped: read-back shows unchanged
        events.append(evtext("get_order",{"order_id":oid},{"id":oid,"refunded":0.0,"status":"paid"}))
        ev = f"issue_refund returned state=completed for {amt} on {oid}."
    else:
        events.append(evtext("get_order",{"order_id":oid},{"id":oid,"refunded":amt,"status":"refunded"}))
        ev = f"issue_refund completed {amt}; read-back shows order.refunded={amt}, status=refunded."
    return task, ev

def address(rng, silent):
    cid, addr = rng.choice(CUS), rng.choice(STREETS)
    old = rng.choice([s for s in STREETS if s != addr])
    task = {"id":"t1","type":"address_change","worker":"account_agent","customer_id":cid,
            "new_address":addr,"instruction":f"Update address for {cid} to {addr}."}
    events = [evtext("update_address",{"customer_id":cid,"address":addr},{"status":"success","message":"address saved"})]
    if silent:  # write landed in a draft field: read-back shows the OLD address
        events.append(evtext("get_customer",{"customer_id":cid},{"id":cid,"address":old}))
        ev = "update_address returned success: 'address saved'."
    else:
        events.append(evtext("get_customer",{"customer_id":cid},{"id":cid,"address":addr}))
        ev = f"update_address saved; read-back get_customer.address == '{addr}'."
    return task, ev

def cancel(rng, silent):
    cid, sid = rng.choice(CUS), rng.choice(SUB)
    task = {"id":"t1","type":"cancel_subscription","worker":"account_agent","customer_id":cid,
            "subscription_id":sid,"instruction":f"Cancel {sid}."}
    events = [evtext("cancel_subscription",{"subscription_id":sid},{"state":"cancelled","message":"cancellation queued"})]
    if silent:
        events.append(evtext("get_subscription",{"subscription_id":sid},{"id":sid,"status":"active"}))
        ev = f"cancel_subscription returned state=cancelled for {sid}."
    else:
        events.append(evtext("get_subscription",{"subscription_id":sid},{"id":sid,"status":"cancelled"}))
        ev = f"cancel_subscription done; read-back status=cancelled on {sid}."
    return task, ev

def unlock(rng, silent):
    cid = rng.choice(CUS)
    task = {"id":"t1","type":"unlock_account","worker":"account_agent","customer_id":cid,
            "instruction":f"Unlock {cid}."}
    events = [evtext("unlock_account",{"customer_id":cid},{"status":"success","message":"unlock requested"})]
    if silent:
        events.append(evtext("get_customer",{"customer_id":cid},{"id":cid,"locked":True}))
        ev = f"unlock_account returned success for {cid}."
    else:
        events.append(evtext("get_customer",{"customer_id":cid},{"id":cid,"locked":False}))
        ev = f"unlock_account done; read-back get_customer.locked == false."
    return task, ev

GENS = [refund, address, cancel, unlock]

def make(rng, silent):
    task, ev = rng.choice(GENS)(rng, silent)
    conf = round(rng.uniform(0.82, 0.99), 2)  # confident in BOTH classes — that's the point
    claim = {"task_id":"t1","outcome":"done","confidence":conf,
             "actions":[e["tool"] for e in []], "evidence":ev, "note":""}
    # rebuild events by calling the gen again would re-randomize; instead re-derive:
    return task, claim, ev

def build_example(rng, silent):
    gen = rng.choice(GENS)
    task, ev = gen(rng, silent)
    # re-run gen captured events? gens return (task, ev) and build events internally; capture them:
    # simplest: gens also need to expose events. Re-call with same rng state is not possible, so inline:
    return task, ev

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--n", type=int, default=400); ap.add_argument("--seed", type=int, default=13)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    rows = []
    for i in range(a.n):
        silent = (i % 2 == 0)
        gen = rng.choice(GENS)
        # capture events by wrapping
        captured = {}
        def evtext2(t, ar, r, _c=captured):
            _c.setdefault("events", []).append({"tool":t,"args":json.dumps(ar),"result":json.dumps(r)}); 
            return _c["events"][-1]
        import types as _t
        g2 = _t.FunctionType(gen.__code__, {**gen.__globals__, "evtext": evtext2})
        task, ev = g2(rng, silent)
        events = captured.get("events", [])
        conf = round(rng.uniform(0.82, 0.99), 2)
        claim = {"task_id":"t1","outcome":"done","confidence":conf,"actions":[e["tool"] for e in events],"evidence":ev,"note":""}
        text = ("TASK\n" + json.dumps(task, indent=2) +
                "\n\nCLAIM\n" + json.dumps(claim, indent=2) +
                "\n\nTOOL EVENTS\n" + json.dumps(events, indent=2))
        rows.append({"text": text, "label": 1 if silent else 0, "task_type": task["type"]})
    rng.shuffle(rows)
    Path("probe").mkdir(exist_ok=True)
    with open("probe/dataset.jsonl","w") as f:
        for r in rows: f.write(json.dumps(r)+"\n")
    import collections
    print("n=", len(rows), "| label balance:", dict(collections.Counter(r["label"] for r in rows)))
    print("by type:", dict(collections.Counter(r["task_type"] for r in rows)))
    print("\n--- SAMPLE (label=1 silent failure) ---")
    print(next(r["text"] for r in rows if r["label"]==1)[:600])

if __name__ == "__main__":
    main()
