"""Probe dataset generator. Default = HARD (surface-matched): the label is purely
whether the read-back value equals the requested value. The requested amount appears
as "$490.00" (2dp) and the read-back as "490.0" (1dp): same value, DIFFERENT tokens,
so a bag-of-words model can't match across formats, and no single token is class-
diagnostic (both the requested value and the read-back value range over the shared
pool in both classes). Only a model that represents numeric equality can separate them.
--easy reproduces the original lexically-separable set."""
import json, random, argparse, collections
from pathlib import Path

CUS=[f"cus_{n}" for n in range(1001,1099)]
ORD=[f"ord_{n}" for n in range(5001,5099)]
AMTS=[19.0,29.0,39.0,49.0,59.0,80.0,99.0,120.0,149.0,199.0,249.0,299.0,399.0,490.0,599.0]

def hard_example(rng, silent):
    R=rng.choice(AMTS); oid=rng.choice(ORD); cid=rng.choice(CUS)
    rb = R if not silent else rng.choice([a for a in AMTS+[0.0] if abs(a-R)>1e-6])
    task={"id":"t1","type":"refund","worker":"billing_agent","customer_id":cid,"order_id":oid,
          "instruction":f"Refund the ${R:.2f} charge on {oid} in full."}
    events=[{"tool":"get_order","args":json.dumps({"order_id":oid}),
             "result":json.dumps({"id":oid,"total":f"{R:.2f}","status":"paid"})},
            {"tool":"issue_refund","args":json.dumps({"order_id":oid,"amount":f"{R:.2f}"}),
             "result":json.dumps({"state":"completed","message":"refund submitted"})},
            {"tool":"get_order","args":json.dumps({"order_id":oid}),
             "result":json.dumps({"id":oid,"refunded":f"{rb:.1f}"})}]
    claim={"task_id":"t1","outcome":"done","confidence":round(rng.uniform(0.82,0.99),2),
           "actions":["get_order","issue_refund","get_order"],
           "evidence":f"issue_refund returned state=completed; read-back order.refunded={rb:.1f}.","note":""}
    return task, claim, events

def easy_example(rng, silent):
    R=rng.choice(AMTS); oid=rng.choice(ORD); cid=rng.choice(CUS)
    task={"id":"t1","type":"refund","worker":"billing_agent","customer_id":cid,"order_id":oid,"amount":R,
          "instruction":f"Refund {R} on {oid}."}
    if silent:
        rb=0.0; ev=f"issue_refund returned state=completed for {R} on {oid}."; status="paid"
    else:
        rb=R; ev=f"issue_refund completed {R}; read-back order.refunded={R}, status=refunded."; status="refunded"
    events=[{"tool":"get_order","args":json.dumps({"order_id":oid}),"result":json.dumps({"id":oid,"refunded":rb,"status":status})}]
    claim={"task_id":"t1","outcome":"done","confidence":round(rng.uniform(0.82,0.99),2),"actions":["get_order","issue_refund"],"evidence":ev,"note":""}
    return task, claim, events

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--n",type=int,default=400); ap.add_argument("--seed",type=int,default=13); ap.add_argument("--easy",action="store_true")
    a=ap.parse_args(); rng=random.Random(a.seed); rows=[]
    gen = easy_example if a.easy else hard_example
    for i in range(a.n):
        silent=(i%2==0)
        task,claim,events=gen(rng,silent)
        text=("TASK\n"+json.dumps(task,indent=2)+"\n\nCLAIM\n"+json.dumps(claim,indent=2)+
              "\n\nTOOL EVENTS\n"+json.dumps(events,indent=2))
        rows.append({"text":text,"label":1 if silent else 0,"task_type":"refund"})
    rng.shuffle(rows)
    Path("probe").mkdir(exist_ok=True)
    with open("probe/dataset.jsonl","w") as f:
        for r in rows: f.write(json.dumps(r)+"\n")
    print("mode=", "easy" if a.easy else "HARD", "| n=",len(rows),"| balance:",dict(collections.Counter(r["label"] for r in rows)))
    print("\n--- one genuine (label 0) vs one silent (label 1), note $X.00 vs X.0 ---")
    g=next(r for r in rows if r["label"]==0); s=next(r for r in rows if r["label"]==1)
    for tag,r in [("GENUINE",g),("SILENT",s)]:
        instr=json.loads(r["text"].split("TASK\n",1)[1].split("\n\nCLAIM",1)[0])["instruction"]
        rb=json.loads(r["text"].rsplit("refunded\": \"",1)[-1].split('"',1)[0]) if False else r["text"].rsplit('refunded": "',1)[-1].split('"',1)[0]
        print(f"  {tag}: instruction={instr!r}  readback.refunded={rb!r}")

if __name__=="__main__": main()
