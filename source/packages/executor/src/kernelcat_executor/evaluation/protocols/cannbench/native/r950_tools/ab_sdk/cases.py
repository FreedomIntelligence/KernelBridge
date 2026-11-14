"""Pull public case catalogs and per-case A/B / official-job numbers."""
from __future__ import annotations

import ast
import base64
import csv
import json
import re
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import CAMPAIGN, TOOLS, Config
from .job import canon_op

CASES_ROOT = TOOLS / "cases"


def case_index(cid) -> int | None:
    if cid is None:
        return None
    s = str(cid).strip()
    if s.isdigit():
        return int(s)
    m = re.search(r"(\d+)$", s.replace(".json", ""))
    return int(m.group(1)) if m else None


def _lit(s: str):
    if not s:
        return None
    t = s.replace("null", "None")
    try:
        return ast.literal_eval(t)
    except (SyntaxError, ValueError):
        return s


def s_of(B, C, H) -> float | None:
    try:
        B, C, H = float(B), float(C), float(H)
    except (TypeError, ValueError):
        return None
    d = (C - H) + (B - H)
    if d <= 0:
        return 1.0
    return (B - H) / d


@dataclass
class CaseSpec:
    op: str
    case_id: int
    input_shape: object = None
    dtype: object = None
    attrs: object = None
    value_range: object = None
    note: str = ""


@dataclass
class CaseHit:
    case_id: str
    idx: int | None
    elapsed_us: float | None = None
    passed: bool | None = None
    status: str = ""
    B: float | None = None
    H: float | None = None
    s: float | None = None
    speedup: float | None = None
    kernels: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def catalog(op: str) -> list[CaseSpec]:
    camel = canon_op(op)
    csv_path = None
    for d in CASES_ROOT.iterdir():
        if not d.is_dir():
            continue
        p = d / "cases.csv"
        if not p.is_file():
            continue
        try:
            if canon_op(d.name) == camel:
                csv_path = p
                break
        except KeyError:
            continue
    if csv_path is None:
        raise FileNotFoundError(f"no cases.csv for {camel} under {CASES_ROOT}")
    rows: list[CaseSpec] = []
    # The catalog is repository data and is always UTF-8.  Relying on the
    # Windows locale (typically GBK) makes otherwise-valid operator rows fail
    # before an A/B run even starts.
    with csv_path.open(newline="", encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            rows.append(
                CaseSpec(
                    op=camel,
                    case_id=int(rec["case_id"]),
                    input_shape=_lit(rec.get("input_shape") or ""),
                    dtype=_lit(rec.get("dtype") or ""),
                    attrs=_lit(rec.get("attrs") or ""),
                    value_range=_lit(rec.get("value_range") or ""),
                    note=(rec.get("note") or "").strip(),
                )
            )
    return rows


def parse_eval_obj(data: dict) -> dict:
    """cann_final_eval JSON or job.results."""
    ops = data.get("operators") or (data.get("results") or {}).get("operators") or []
    out_ops = []
    for o in ops:
        hits: list[CaseHit] = []
        for c in o.get("cases") or []:
            acc = c.get("accuracy") or {}
            passed = acc.get("passed")
            if passed is None:
                passed = c.get("status") in ("success", "passed", True)
            C = c.get("elapsed_us")
            B = c.get("baseline_perf_us")
            H = c.get("t_hw_us")
            s = c.get("perf_score")
            if s is None:
                s = s_of(B, C, H)
            kernels = (c.get("op_times") or {}).get("device_kernels") or {}
            cid = str(c.get("case_id") or "")
            hits.append(
                CaseHit(
                    case_id=cid,
                    idx=case_index(cid),
                    elapsed_us=float(C) if isinstance(C, (int, float)) else None,
                    passed=bool(passed) if passed is not None else None,
                    status=str(c.get("status") or ""),
                    B=float(B) if isinstance(B, (int, float)) else None,
                    H=float(H) if isinstance(H, (int, float)) else None,
                    s=float(s) if isinstance(s, (int, float)) else None,
                    speedup=c.get("speedup"),
                    kernels=dict(kernels) if isinstance(kernels, dict) else {},
                )
            )
        out_ops.append({
            "operator": o.get("operator") or o.get("operator_name") or "",
            "score": o.get("score"),
            "function_score": o.get("function_score"),
            "avg_speedup": o.get("avg_speedup"),
            "passed_cases": o.get("passed_cases"),
            "total_cases": o.get("total_cases") or len(hits),
            "cases": hits,
        })
    return {"operators": out_ops}


def parse_eval_file(path: str | Path) -> dict:
    return parse_eval_obj(json.loads(Path(path).read_text()))


def min_elapsed(hits: list[CaseHit]) -> dict[int, float]:
    acc: dict[int, list[float]] = {}
    for h in hits:
        if h.idx is None or h.elapsed_us is None or h.elapsed_us <= 0:
            continue
        acc.setdefault(h.idx, []).append(h.elapsed_us)
    return {i: min(vs) for i, vs in acc.items()}


def diff_elapsed(
    base: dict[int, float],
    mt: dict[int, float],
    specs: list[CaseSpec] | None = None,
) -> list[dict]:
    notes = {s.case_id: s.note for s in specs} if specs else {}
    shapes = {s.case_id: s.input_shape for s in specs} if specs else {}
    ids = sorted(set(base) | set(mt))
    rows = []
    for i in ids:
        b, m = base.get(i), mt.get(i)
        ratio = None
        if b and m and m > 0:
            ratio = round(b / m, 4)
        rows.append({
            "idx": i,
            "base_us": b,
            "mt_us": m,
            "x": ratio,
            "shape": shapes.get(i),
            "note": notes.get(i, ""),
        })
    return rows


def fire_rows(diff: list[dict], *, min_x: float = 1.05) -> list[dict]:
    return [r for r in diff if r.get("x") is not None and r["x"] >= min_x]


_PULL_PY = r"""
import json, glob, os, sys
root = sys.argv[1]
out = []
for fs in sorted(glob.glob(root + "/rep_*/cann_final_eval*.json")):
    d = json.load(open(fs))
    armrep = os.path.basename(os.path.dirname(fs))
    ops = d.get("operators") or []
    rec = {"dir": armrep, "file": os.path.basename(fs), "operators": ops}
    out.append(rec)
print(json.dumps(out))
"""


def pull_share_run(tag: str, cfg: Config | None = None) -> dict:
    """One SSH: dump every cann_final_eval under the configured share runs directory."""
    from .ssh import ssh
    cfg = cfg or Config()
    root = f"{cfg.share_runs}/{tag}"
    b64 = base64.b64encode(_PULL_PY.encode()).decode("ascii")
    r = ssh(
        f"printf '%s' '{b64}' | base64 -d | python3 - {shlex.quote(root)}",
        cfg,
        timeout=60,
    )
    text = r.stdout.strip()
    # last JSON array
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < start:
        raise RuntimeError(f"no eval dump for run {tag}: {text[-400:]}")
    blobs = json.loads(text[start:end + 1])
    arms: dict[str, list[dict]] = {}
    op_name = ""
    for blob in blobs:
        parsed = parse_eval_obj(blob)
        name = blob.get("dir") or ""
        # rep_base_1 -> base
        arm = name
        if name.startswith("rep_"):
            parts = name.split("_")
            arm = "_".join(parts[1:-1]) if len(parts) >= 3 else name[4:]
        arms.setdefault(arm, []).extend(parsed["operators"])
        if parsed["operators"]:
            op_name = parsed["operators"][0].get("operator") or op_name
    return {"tag": tag, "op": op_name, "arms": _serialize_arms(arms)}


def _serialize_arms(arms: dict[str, list[dict]]) -> dict:
    out = {}
    for arm, ops in arms.items():
        hits: list[CaseHit] = []
        scores = []
        for o in ops:
            scores.append(o.get("score"))
            for h in o.get("cases") or []:
                hits.append(h if isinstance(h, CaseHit) else CaseHit(
                    case_id=str(h.get("case_id") or ""),
                    idx=case_index(h.get("case_id")),
                    elapsed_us=h.get("elapsed_us"),
                    passed=h.get("passed"),
                    status=str(h.get("status") or ""),
                    B=h.get("B") or h.get("baseline_perf_us"),
                    H=h.get("H") or h.get("t_hw_us"),
                    s=h.get("s") or h.get("perf_score"),
                    speedup=h.get("speedup"),
                    kernels=h.get("kernels") or {},
                ))
        out[arm] = {
            "scores": [s for s in scores if s is not None],
            "min_elapsed": min_elapsed(hits),
            "cases": [h.to_dict() for h in hits],
        }
    return out


def analyze_arms(arm_map: dict, op: str | None = None) -> dict:
    """arm_map from pull_share_run['arms'] or local files."""
    specs = catalog(op) if op else []
    names = list(arm_map)
    base_name = "base" if "base" in arm_map else (names[0] if names else "")
    result = {"op": op, "base": base_name, "diff": {}}
    if not base_name:
        return result
    base_el = {int(k): float(v) for k, v in (arm_map[base_name].get("min_elapsed") or {}).items()}
    for name in names:
        if name == base_name:
            continue
        mt_el = {int(k): float(v) for k, v in (arm_map[name].get("min_elapsed") or {}).items()}
        d = diff_elapsed(base_el, mt_el, specs)
        xs = [r["x"] for r in d if r.get("x")]
        result["diff"][name] = {
            "rows": d,
            "fire_1_05": fire_rows(d, min_x=1.05),
            "fire_1_08": fire_rows(d, min_x=1.08),
            "median_x": sorted(xs)[len(xs) // 2] if xs else None,
            "n": len(d),
        }
    return result


def parse_env_file(text: str) -> dict[str, str]:
    """Bash `source` semantics: last assignment wins. Does not override process env."""
    file_env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, _, v = line.partition("=")
        file_env[k.strip()] = v.strip().strip("'").strip('"')
    return file_env


def load_campaign_env() -> dict[str, str]:
    import os
    env_path = CAMPAIGN / "cann.env"
    file_env = parse_env_file(env_path.read_text()) if env_path.is_file() else {}
    for k, v in file_env.items():
        os.environ.setdefault(k, v)
    os.environ.setdefault("CANN_BOARD", "910c")
    return file_env


def gitcode_tokens() -> list[tuple[str, str]]:
    """Unique tokens in campaign account order. Values never logged."""
    import os
    load_campaign_env()
    keys = ["GITCODE_TOKEN"] + [f"GITCODE_TOKEN_{i}" for i in range(2, 20)]
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for k in keys:
        t = os.environ.get(k) or ""
        if t and t not in seen:
            seen.add(t)
            out.append((k, t))
    return out


def pull_job(job_id: str) -> dict:
    """Official job per-case via wave.job_meta.

    Non-owner tokens return status/score but `results: null`. Scan all
    GITCODE_TOKEN* until operators[].cases is populated (GET only, 0 credit).
    """
    tokens = gitcode_tokens()
    if not tokens:
        raise RuntimeError("no GITCODE_TOKEN in env/cann.env")
    sys_path_tools = str(TOOLS)
    if sys_path_tools not in __import__("sys").path:
        __import__("sys").path.insert(0, sys_path_tools)
    import wave as W
    meta: dict = {}
    owner = ""
    for name, tok in tokens:
        m = W.job_meta(job_id, tok) or {}
        ops = ((m.get("results") or {}).get("operators") or [])
        if ops:
            meta, owner = m, name
            break
        if m.get("status") and not meta:
            meta, owner = m, name
    if not meta:
        raise RuntimeError(f"job {job_id}: GET empty on {len(tokens)} accounts")
    parsed = parse_eval_obj({"results": meta.get("results") or meta})
    return {
        "job_id": job_id,
        "status": meta.get("status"),
        "score": meta.get("result_score") or meta.get("score"),
        "case_set": meta.get("case_set"),
        "owner": owner,
        "operators": [
            {
                **{k: v for k, v in o.items() if k != "cases"},
                "cases": [h.to_dict() for h in o["cases"]],
            }
            for o in parsed["operators"]
        ],
    }
