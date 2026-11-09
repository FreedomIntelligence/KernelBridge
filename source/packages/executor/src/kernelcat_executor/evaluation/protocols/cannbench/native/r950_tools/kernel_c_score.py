#!/usr/bin/env python3
"""Per-case kernel C → s → row points. Speedup x is diagnostic only.

Row: 20 + 30*(n_pass/20) + 50*mean(s_i), n=20, one case = 2.50*s.
s = (B-H)/((C-H)+(B-H)), H=B/10 default.
C = kernel_details.csv Duration(us) median sum, not elapsed, not x=B/C.

A fail zeros that case's 2.50*s and subtracts 1.50 from the pass term.
3-case probe contribution = sum of those cases' 2.50*Δs (other 17 treated as 0).
Do not average speedups. Do not use 2.50*mean(Δs_3) as the row delta.
"""
from __future__ import annotations

import argparse
import json
import sys

CASE_PERF = 50.0 / 20.0  # 2.50
CASE_PASS = 30.0 / 20.0  # 1.50


def s_of(c: float, b: float, h: float | None = None) -> float:
    if b <= 0:
        raise ValueError("B must be > 0")
    h = b / 10.0 if h is None else h
    den = (c - h) + (b - h)
    if den <= 0:
        return 0.0
    return (b - h) / den


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "per-case kernel-C paper score. "
            "C is kernel_details.csv Duration(us) median (not elapsed, not speedup x=B/C). "
            "3-case probe contribution uses those Duration medians."
        )
    )
    p.add_argument("--json", help='[{"id","role","B","C_parent","C_arm","H"?,"pass_parent"?,"pass_arm"?}]')
    p.add_argument("--row", action="append", default=[],
                   help="id,role,B,C_parent,C_arm[,pass_arm 1|0]")
    p.add_argument("--n", type=int, default=20, help="official case count (default 20)")
    args = p.parse_args()
    rows = []
    if args.json:
        rows.extend(json.loads(args.json))
    for r in args.row:
        parts = r.split(",")
        if len(parts) < 5:
            print("row: id,role,B,C_parent,C_arm[,pass_arm]", file=sys.stderr)
            return 2
        rec = {
            "id": parts[0], "role": parts[1],
            "B": float(parts[2]), "C_parent": float(parts[3]), "C_arm": float(parts[4]),
        }
        if len(parts) >= 6:
            rec["pass_arm"] = parts[5] not in ("0", "false", "False")
        rows.append(rec)
    if not rows:
        print("pass --row id,role,B,C_parent,C_arm[,pass_arm]", file=sys.stderr)
        return 2
    n = args.n
    perf_w = 50.0 / n
    pass_w = 30.0 / n
    print(f"{'id':>6} {'role':<10} {'B':>8} {'C_par':>8} {'C_arm':>8} "
          f"{'x':>6} {'s_par':>7} {'s_arm':>7} {'Δs':>8} {'Δperf':>7} {'Δpass':>7} {'Δrow':>7}")
    d_perf = d_pass = 0.0
    for row in rows:
        b = float(row["B"])
        cp = float(row["C_parent"])
        ca = float(row["C_arm"])
        h = float(row["H"]) if row.get("H") is not None else None
        passed = bool(row.get("pass_arm", True))
        sp = s_of(cp, b, h)
        sa = s_of(ca, b, h) if passed else 0.0
        x = (cp / ca) if ca else float("inf")
        ds = sa - sp
        dperf = perf_w * ds
        dpass = 0.0 if passed else -pass_w
        drow = dperf + dpass
        d_perf += dperf
        d_pass += dpass
        print(f"{str(row['id']):>6} {row['role']:<10} {b:8.1f} {cp:8.1f} {ca:8.1f} "
              f"{x:6.3f} {sp:7.3f} {sa:7.3f} {ds:+8.3f} {dperf:+7.3f} {dpass:+7.3f} {drow:+7.3f}")
    print(f"measured n={len(rows)}/{n}  ΣΔperf={d_perf:+.3f}  ΣΔpass={d_pass:+.3f}  "
          f"row Δ from these cases={d_perf+d_pass:+.3f}")
    print("x is diagnostic (hit>1.02 + new Name; fallback/tax |x-1|<0.02). "
          "Do not average x. Unmeasured cases contribute 0 until probed.")
    print("KEEP is still live BEST-of. This is a veto / paper estimate, not a submit ticket.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
