#!/usr/bin/env python3
"""Report MLA family probes. Authority is kernel_details Duration(us).

Do not nest this in a bash heredoc — f-strings there broke sibling harnesses.
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


SKIP = ("CannBenchCacheClean", "cann_bench", "Profiler", "Step")


def _cid(case_id) -> int:
    s = str(case_id)
    return int(s.rsplit("_", 1)[-1])


def _route(name: str) -> str:
    n = name.lower()
    if "qresident" in n:
        return "qresident"
    if "mixed" in n or "ring" in n:
        return "mixed"
    if "qk_cube" in n:
        return "qk"
    if "softmax" in n:
        return "softmax"
    if "pv_cube" in n:
        return "pv"
    if "mla_kernel" in n:
        return "vector"
    return "aux"


def kernel_c_for_case(rep: Path, cid: int) -> dict[str, list[float]]:
    by: dict[str, list[float]] = defaultdict(list)
    files = list(rep.glob(f"prof_data/**/mla/{cid}/**/kernel_details.csv"))
    if not files:
        files = list(rep.glob(f"**/mla/{cid}/**/kernel_details.csv"))
    if not files:
        files = list(rep.glob(f"**/{cid}/**/kernel_details.csv"))
    for path in files:
        with path.open(errors="ignore") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("Name") or row.get("name") or "").strip()
                dur = row.get("Duration(us)") or row.get("Duration(us )") or row.get("duration")
                if not name or dur in (None, ""):
                    continue
                if any(s.lower() in name.lower() for s in SKIP):
                    continue
                try:
                    by[name].append(float(dur))
                except ValueError:
                    continue
    return by


def kernel_c_all(rep: Path) -> dict[str, list[float]]:
    by: dict[str, list[float]] = defaultdict(list)
    for path in rep.glob("**/kernel_details.csv"):
        with path.open(errors="ignore") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("Name") or row.get("name") or "").strip()
                dur = row.get("Duration(us)") or row.get("Duration(us )") or row.get("duration")
                if not name or dur in (None, ""):
                    continue
                if any(s.lower() in name.lower() for s in SKIP):
                    continue
                try:
                    by[name].append(float(dur))
                except ValueError:
                    continue
    return by


def report(run: Path, perf: str) -> int:
    rep = run / "reports"
    log = run / "evaluation.log"
    if log.is_file():
        text = log.read_text(errors="ignore")
        if any(s in text for s in ("507015", "507034", "AI CORE EXCEPTION", "core dump")):
            print("MARK HANG_OR_507015")
            for line in text.splitlines():
                if any(s in line for s in ("507015", "507034", "AI CORE EXCEPTION", "core dump")):
                    print(line)
    fs = sorted(rep.glob("cann_final_eval*.json")) or sorted(rep.glob("cann_*eval*.json"))
    if not fs:
        print("NO_REPORT")
        if log.is_file():
            print(log.read_text(errors="ignore")[-12000:])
        return 1
    d = json.loads(fs[-1].read_text())
    ids = []
    for o in d.get("operators") or []:
        for c in o.get("cases") or []:
            acc = c.get("accuracy") or {}
            ot = (c.get("op_times") or {}).get("device_kernels") or {}
            names = list(ot) or (c.get("device_kernels") or [])
            if isinstance(names, str):
                names = [names]
            routes = sorted({_route(str(n)) for n in names}) or ["-"]
            cid = _cid(c.get("case_id"))
            ids.append(cid)
            print(
                "CASE", c.get("case_id"),
                "passed=", acc.get("passed"),
                "us=", c.get("elapsed_us"),
                "route=", ",".join(routes),
                "err=", acc.get("error_message") or c.get("error_message") or "",
            )
            for k, v in sorted(ot.items(), key=lambda kv: -kv[1])[:8]:
                print("   ", "%.1f" % v, "us", str(k)[:110])
        print(
            "SUMMARY", o.get("passed_cases"), "/", o.get("total_cases"),
            "score=", o.get("score"),
        )
    if perf != "1":
        return 0
    csvs = list(rep.glob("**/kernel_details.csv"))
    print("KERNEL_DETAILS_FILES", len(csvs))
    print("KERNEL_C_BY_CASE")
    any_case = False
    for cid in ids:
        by = kernel_c_for_case(rep, cid)
        if not by:
            print("CASE_C", cid, "NONE")
            continue
        any_case = True
        meds = {name: statistics.median(xs) for name, xs in by.items()}
        sum_med = sum(meds.values())
        top = sorted(by.items(), key=lambda kv: -statistics.median(kv[1]))
        print(
            "CASE_C", cid,
            "sum_med=" + ("%.3f" % sum_med),
            "n_names=" + str(len(top)),
        )
        for name, xs in top[:10]:
            print(
                "  ",
                "kern=" + name[:90],
                "route=" + _route(name),
                "n=" + str(len(xs)),
                "med=" + ("%.3f" % statistics.median(xs)),
            )
    if not any_case:
        print("KERNEL_C_BY_NAME")
        by = kernel_c_all(rep)
        top = sorted(by.items(), key=lambda kv: -statistics.median(kv[1]))
        for name, xs in top[:16]:
            print(
                "NAME_C",
                "kern=" + name[:90],
                "route=" + _route(name),
                "n=" + str(len(xs)),
                "med=" + ("%.3f" % statistics.median(xs)),
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "report" or len(argv) < 2:
        print("usage: mla_family_helper.py report <run-dir> [perf 0|1]", file=sys.stderr)
        return 2
    run = Path(argv[1])
    perf = argv[2] if len(argv) > 2 else "0"
    return report(run, perf)


if __name__ == "__main__":
    raise SystemExit(main())
