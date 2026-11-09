#!/usr/bin/env python3
"""Report 3-case GQA probes. Authority is kernel_details Duration(us) median.

Do not nest this in a bash heredoc — f-strings there broke gqa_dev_cases.sh.
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


SKIP = ("CannBenchCacheClean", "cann_bench", "Profiler", "Step")
ANCHORS = Path("/root/jerry/gap10/gqa_official_anchors.json")


def _anchors() -> dict:
    if ANCHORS.is_file():
        return json.loads(ANCHORS.read_text()).get("cases") or {}
    return {}


def _cid(case_id) -> int:
    s = str(case_id)
    return int(s.rsplit("_", 1)[-1])


def kernel_c_for_case(rep: Path, cid: int) -> dict[str, list[float]]:
    by: dict[str, list[float]] = defaultdict(list)
    files = list(rep.glob(f"prof_data/**/gqa/{cid}/**/kernel_details.csv"))
    if not files:
        files = list(rep.glob(f"**/gqa/{cid}/**/kernel_details.csv"))
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


def report(run: Path, perf: str) -> int:
    rep = run / "reports"
    fs = sorted(rep.glob("cann_final_eval*.json")) or sorted(rep.glob("cann_*eval*.json"))
    if not fs:
        print("NO_REPORT")
        log = run / "evaluation.log"
        if log.is_file():
            print(log.read_text(errors="ignore")[-12000:])
        return 1
    d = json.loads(fs[-1].read_text())
    for o in d.get("operators") or []:
        for c in o.get("cases") or []:
            a = c.get("accuracy") or {}
            names = c.get("device_kernels") or c.get("kernels") or []
            if isinstance(names, str):
                names = [names]
            ot = (c.get("op_times") or {}).get("device_kernels") or {}
            if ot and not names:
                names = list(ot)
            us, B = c.get("elapsed_us"), c.get("baseline_perf_us")
            H = c.get("t_hw_us")
            rec = _anchors().get(str(_cid(c.get("case_id"))))
            if rec and not B:
                B, H = rec.get("B"), rec.get("H")
            try:
                spd = (float(B) / float(us)) if us and B else None
            except (TypeError, ValueError, ZeroDivisionError):
                spd = None
            print(
                "CASE", c.get("case_id"),
                "passed=", a.get("passed"),
                "us=", us,
                "B=", B,
                "H=", c.get("t_hw_us"),
                "spd=", None if spd is None else round(spd, 3),
                "kern=", ",".join(str(x) for x in names[:4]) or "-",
                "err=", a.get("error_message") or c.get("error_message") or "",
            )
        print(
            "SUMMARY", o.get("passed_cases"), "/", o.get("total_cases"),
            "score=", o.get("score"),
        )
    if perf != "1":
        return 0
    csvs = list(rep.glob("**/kernel_details.csv"))
    print("KERNEL_DETAILS_FILES", len(csvs))
    ids = []
    for o in d.get("operators") or []:
        for c in o.get("cases") or []:
            ids.append(_cid(c.get("case_id")))
    if not ids:
        ids = [11, 14, 1]
    print("KERNEL_C_BY_CASE")
    for cid in ids:
        by = kernel_c_for_case(rep, cid)
        if not by:
            print("CASE_C", cid, "NONE")
            continue
        top = sorted(by.items(), key=lambda kv: -statistics.median(kv[1]))
        name, xs = top[0]
        print(
            "CASE_C", cid,
            "kern=" + name[:80],
            "n=" + str(len(xs)),
            "med=" + ("%.2f" % statistics.median(xs)),
            "sum=" + ("%.1f" % sum(xs)),
        )
        for name, xs in top[1:4]:
            print(
                "  also", name[:60],
                "n=" + str(len(xs)),
                "med=" + ("%.2f" % statistics.median(xs)),
            )
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "report":
        print("usage: gqa_family_helper.py report <run-dir> <0|1-perf>", file=sys.stderr)
        return 2
    return report(Path(argv[2]), argv[3] if len(argv) > 3 else "0")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
