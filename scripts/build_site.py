"""Build site/ — a static snapshot of the Attest Fleet console, deployable to any static host.

    python scripts/build_site.py                 # build site/ from data/api-snapshot/
    python scripts/build_site.py --fetch URL     # refresh data/api-snapshot/ from a running app first

The console's read paths are served from JSON files captured from the live service; search,
status filter, sort and paging over runs are done in the page, mirroring GET /runs. The
server-sent board stream and every mutating call (kill switch, approvals, ticket trigger) are
retired: pressing them shows a notice instead of failing.
"""
import json, shutil, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "data" / "api-snapshot"
SITE = ROOT / "site"
HTML = ROOT / "src" / "attest_fleet" / "static" / "index.html"

NOTICE = ("This is a static snapshot of the final run, taken 25 Oct 2026. The fleet service on "
          "Cloud Run is retired, so the kill switch, approvals and ticket trigger no longer act; "
          "the runs, verdicts and evidence are the real ones.")


def fetch(base: str) -> None:
    base = base.rstrip("/")
    get = lambda p: urllib.request.urlopen(f"{base}{p}", timeout=120).read()
    SNAP.mkdir(parents=True, exist_ok=True)
    (SNAP / "runs").mkdir(exist_ok=True)
    (SNAP / "fleet").mkdir(exist_ok=True)
    for path, name in [("/health", "health.json"), ("/metrics", "metrics.json"),
                       ("/runs/interrupted", "runs__interrupted.json"), ("/approvals", "approvals.json"),
                       ("/fleet/identities", "fleet/identities.json"), ("/fleet/playbook", "fleet/playbook.json"),
                       ("/briefing", "briefing.json"), ("/briefing.wav", "briefing.wav"),
                       ("/openapi.json", "openapi.json"), ("/docs", "docs.html")]:
        (SNAP / name).write_bytes(get(path))
    runs = json.loads(get("/runs?limit=100&offset=0"))
    while len(runs["items"]) < runs["total"]:
        page = json.loads(get(f"/runs?limit=100&offset={len(runs['items'])}"))
        if not page["items"]:
            break
        runs["items"] += page["items"]
    (SNAP / "runs.json").write_text(json.dumps(runs))
    for r in runs["items"]:
        (SNAP / "runs" / f"{r['id']}.json").write_bytes(get(f"/runs/{r['id']}"))
        (SNAP / "runs" / f"{r['id']}.evidence.json").write_bytes(get(f"/runs/{r['id']}/evidence"))
    print("fetched", runs["total"], "runs into", SNAP)


API = r'''
/* Static snapshot: reads come from JSON files captured from the live service; writes are retired. */
const SNAP={}; const snap=p=>{ if(!SNAP[p]) SNAP[p]=fetch(p).then(r=>r.ok?r.json():Promise.reject(r)); return SNAP[p]; };
const RETIRED=%s;
const api=async(p,o)=>{
  o=o||{}; if((o.method||"GET").toUpperCase()!=="GET"){ toast(RETIRED); throw new Error("retired"); }
  const [pt,qs]=p.split("?"); const q=new URLSearchParams(qs||"");
  if(pt==="/runs"){
    const all=await snap("/api/runs.json"); let items=all.items.slice();
    const st=q.get("status"), qq=(q.get("q")||"").toLowerCase(), sort=q.get("sort")||"started", order=q.get("order")||"desc";
    if(st) items=items.filter(r=>r.status===st);
    if(qq) items=items.filter(r=>((r.id||"")+((r.ticket||{}).subject||"")+((r.ticket||{}).customer_ref||"")).toLowerCase().includes(qq));
    const key={started:r=>r.started_at||"",subject:r=>((r.ticket||{}).subject||"").toLowerCase(),status:r=>r.status||"",tasks:r=>(r.results||[]).length}[sort]||(r=>r.started_at||"");
    items.sort((a,b)=>{const x=key(a),y=key(b); return (x>y?1:x<y?-1:0)*(order==="asc"?1:-1);});
    const total=items.length, limit=Math.max(1,Math.min(+q.get("limit")||20,100)), offset=+q.get("offset")||0;
    return {items:items.slice(offset,offset+limit),total,limit,offset,counts:all.counts};
  }
  if(pt==="/runs/interrupted") return snap("/api/runs__interrupted.json");
  if(pt.startsWith("/runs/")) return snap("/api/runs/"+encodeURIComponent(pt.slice(6))+".json");
  if(pt==="/approvals"){ const a=await snap("/api/approvals.json"); const st=q.get("status"); return st?a.filter(x=>x.status===st):a; }
  return snap("/api"+pt+".json");
};
'''


def build() -> None:
    h = HTML.read_text()
    start = h.index("const api=(p,o)=>")
    end = h.index("\n", start)
    h = h[:start] + API % json.dumps(NOTICE) + h[end:]
    for a, b in [
        ('fetch("/briefing")', 'fetch("/api/briefing.json")'),
        ('<a href="/metrics">/metrics</a><a href="/fleet/identities">/identities</a>',
         '<a href="/api/metrics.json">/metrics</a><a href="/api/fleet/identities.json">/identities</a>'),
        ('<body>', '<body>\n<div class="snapshot-note" role="note">' + NOTICE + ' <a href="https://github.com/rodriguescarson/attest-fleet">Run it yourself from the repo.</a></div>'),
        ('}catch(e){toast("Action failed")}', '}catch(e){toast(RETIRED)}'),
        ('setTimeout(loadMetrics,1500);}catch(e){toast("Failed")}}', 'setTimeout(loadMetrics,1500);}catch(e){toast(RETIRED)}}'),
        ('toast("Rejected"); loadApprovals();}catch(e){toast("Failed")}}', 'toast("Rejected"); loadApprovals();}catch(e){toast(RETIRED)}}'),
        ('</style>', '.snapshot-note{font:500 13px/1.45 system-ui,sans-serif;padding:10px 16px;background:var(--warn,#c7891c);color:#111;text-align:center}.snapshot-note a{color:inherit}\n</style>'),
    ]:
        assert a in h, a
        h = h.replace(a, b, 1)
    # No stream behind a static site: mark the board as a snapshot and skip the fallback poll.
    s = h.index("  let streamLive=false;\n  try{\n    const es=new EventSource")
    e = h.index("  setInterval(()=>{ if(!streamLive", s)
    e = h.index("\n", e) + 1
    h = h[:s] + '  { const d=$("#liveDot"); if(d) d.title="snapshot of the final run"; }\n' + h[e:]

    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "api" / "runs").mkdir(parents=True)
    (SITE / "api" / "fleet").mkdir(parents=True)
    (SITE / "docs").mkdir(parents=True)
    (SITE / "index.html").write_text(h)
    for name in ["health.json", "metrics.json", "runs.json", "runs__interrupted.json", "approvals.json",
                 "briefing.json", "fleet/identities.json", "fleet/playbook.json"]:
        shutil.copy(SNAP / name, SITE / "api" / name)
    for p in (SNAP / "runs").glob("*.json"):
        shutil.copy(p, SITE / "api" / "runs" / p.name)
    shutil.copy(SNAP / "briefing.wav", SITE / "briefing.wav")
    shutil.copy(SNAP / "openapi.json", SITE / "openapi.json")
    shutil.copy(SNAP / "docs.html", SITE / "docs" / "index.html")
    # Every read path the page can ask for must exist as a file.
    runs = json.loads((SNAP / "runs.json").read_text())
    missing = [f"api/runs/{r['id']}.json" for r in runs["items"] if not (SITE / "api" / "runs" / f"{r['id']}.json").exists()]
    assert not missing, missing
    print("wrote", SITE, "with", runs["total"], "runs")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fetch":
        fetch(sys.argv[2])
    build()
