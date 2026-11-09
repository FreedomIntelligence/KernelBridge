"""Fire the 10 clean-candidate A/B batch on KTP (npu=2 each, parallel), dump launches."""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ab_sdk import AbClient
from ab_sdk.job import AbSpec

clean = json.load(open("/tmp/ab_clean.json"))
specs = []
for c in clean:
    short = c["arm"].replace("_v1", "").replace("_", "")[:16]
    specs.append(AbSpec(
        op=c["op"], reps=2,
        arms={"base": c["base"], "arm": c["tree"]},
        tag=f"scan-{short}",
    ))
print(f"built {len(specs)} specs", flush=True)
cli = AbClient()
t0 = time.time()
launches = cli.ktp.batch_ab(specs, parallel=1, wait=True, sync=True)
dt = time.time() - t0
rows = []
for ln in launches:
    rows.append({"op": ln.spec.op, "tag": ln.spec.tag,
                 "job_id": ln.job_id, "log": ln.log_path})
    print(f"  {ln.spec.op:22} {ln.spec.tag:20} id={ln.job_id or 'NONE'}", flush=True)
Path("/tmp/scan10_launches.json").write_text(json.dumps(rows, indent=2))
ids = [r["job_id"] for r in rows if r["job_id"]]
print(f"\nSUBMIT_DONE {len(ids)}/{len(specs)} ids in {dt:.0f}s -> /tmp/scan10_launches.json", flush=True)
