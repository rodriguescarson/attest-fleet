"""Phase 2+3 (runs on the GPU pod). Reads probe/dataset.jsonl, extracts hidden-state
activations from an open model, trains a linear probe to predict silent failure, and
reports it against three honest controls:
  - shuffled-label null  (should be ~0.5: proves we're not fitting noise)
  - TF-IDF surface baseline (does the representation beat surface words? Advani's question)
  - stated-confidence AUROC (the worker's self-report = the behavioral signal)
The probe-vs-confidence gap IS the representation-action gap (Paper 17): the internals
know something the spoken confidence does not.

  MODEL_ID env overrides the model (default ungated Qwen2.5-1.5B-Instruct; set to a Gemma
  id with an HF token for the Gemma-specific run)."""
import os, json, numpy as np, torch
from sklearn.linear_model import LogisticRegression
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.model_selection import cross_val_score, StratifiedKFold
from transformers import AutoTokenizer, AutoModelForCausalLM

MODEL_ID = os.getenv("MODEL_ID", "Qwen/Qwen2.5-1.5B-Instruct")
rows = [json.loads(l) for l in open("probe/dataset.jsonl")]
texts = [r["text"] for r in rows]
y = np.array([r["label"] for r in rows])
conf = np.array([json.loads(r["text"].split("CLAIM\n",1)[1].split("\n\nTOOL",1)[0])["confidence"] for r in rows])
print(f"model={MODEL_ID}  n={len(rows)}  pos={int(y.sum())}", flush=True)

tok = AutoTokenizer.from_pretrained(MODEL_ID)
model = AutoModelForCausalLM.from_pretrained(MODEL_ID, torch_dtype=torch.float16, output_hidden_states=True,
                                             device_map="cuda")
model.eval()

@torch.no_grad()
def acts(text):
    ids = tok(text, return_tensors="pt", truncation=True, max_length=640).to("cuda")
    hs = model(**ids).hidden_states  # tuple(L+1) each [1,T,d]
    return [h[0].mean(0).float().cpu().numpy() for h in hs]  # mean-pool per layer

# extract once, keep all layers
allh = [acts(t) for t in texts]
L = len(allh[0]); print(f"layers={L}", flush=True)
cv = StratifiedKFold(5, shuffle=True, random_state=0)
def auroc(X, yy): return cross_val_score(LogisticRegression(max_iter=2000, C=1.0), X, yy, cv=cv, scoring="roc_auc").mean()

# probe per layer, pick best
best=(0,-1)
for layer in range(1, L):
    X = np.stack([h[layer] for h in allh]); X = (X - X.mean(0)) / (X.std(0)+1e-6)
    a = auroc(X, y)
    if a > best[1]: best=(layer, a)
    if layer % max(1,(L//6))==0: print(f"  layer {layer:2d}  probe AUROC={a:.3f}", flush=True)
blayer, bauroc = best
Xb = np.stack([h[blayer] for h in allh]); Xb=(Xb-Xb.mean(0))/(Xb.std(0)+1e-6)
null = auroc(Xb, np.random.RandomState(1).permutation(y))
tfidf = auroc(TfidfVectorizer(max_features=4000).fit_transform(texts).toarray(), y)
conf_auroc = auroc(conf.reshape(-1,1), y)

print("\n===== RESULTS =====")
print(f"probe (best layer {blayer}/{L})   AUROC = {bauroc:.3f}")
print(f"shuffled-label null              AUROC = {null:.3f}   (want ~0.50)")
print(f"TF-IDF surface baseline          AUROC = {tfidf:.3f}")
print(f"stated-confidence (self-report)  AUROC = {conf_auroc:.3f}")
print(f"representation-action gap = probe - confidence = {bauroc - conf_auroc:+.3f}")
json.dump({"model":MODEL_ID,"n":len(rows),"best_layer":blayer,"n_layers":L,
           "probe_auroc":round(float(bauroc),4),"null_auroc":round(float(null),4),
           "tfidf_auroc":round(float(tfidf),4),"confidence_auroc":round(float(conf_auroc),4),
           "rep_action_gap":round(float(bauroc-conf_auroc),4)}, open("probe/result.json","w"), indent=2)
print("\nwrote probe/result.json")
