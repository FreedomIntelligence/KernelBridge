#!/usr/bin/env python3
"""零 credit：910c 逐 case 算「把耗时加速 k 倍能换多少分」，给 106 行排序。

⚠ 为什么不能用行级聚合数（avg_speedup / avg_speedup_vs_hw）反推：
SOL 行分是 **逐 case 分的平均**，而逐 case 分是饱和的

    score_i = (B-H)/((C-H)+(B-H)) = 1/(1+u_i),  u_i = (C_i-H_i)/(B_i-H_i)

    mean_i 1/(1+u_i)  !=  1/(1+u_aggregate)          ← Jensen

用聚合数反推 resizebilinear|hidden 会算出现分 91.3，而真实行分是 59.6 ——
差 32 分。用它排序会把「少数 case 极慢、其余接近 roofline」的行误判成
「整体接近 roofline、没头寸」。本脚本只用逐 case 数。

行分 = 20*compile + 30*pass_rate + 50*mean_i(score_i)。

  fruit910.py [--refresh] [k ...]
"""
import json, os, pathlib, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CANN_BOARD", "910c")
import wave as W

CACHE = pathlib.Path(os.path.dirname(os.path.abspath(__file__))) / "rows910.json"
KS = [1.3, 1.5, 2.0]


def fetch():
    # Keep read visibility aligned with the canonical 910C account pool.  Jobs
    # may expose detailed results to only one campaign account, so omitting a
    # newly added token can make a valid current component look unreadable.
    toks = [os.environ[a] for a in W.ACCTS if os.environ.get(a)]
    comps = []
    for tok in toks:
        s, d = W.api(f"/api/leaderboard/solution/{W.SOL}/components", tok)
        comps = (d.get("components") or d.get("entries") or []) \
            if s == 200 and isinstance(d, dict) else []
        if comps:
            break
    if not comps:
        raise RuntimeError("no configured 910C account can read solution components")
    out = {}
    for n, c in enumerate(comps, 1):
        key = f"{c['operator_key']}|{c['case_set']}"
        jid = c["job_id"]
        cases = None
        # results 的可见性按账号走，不按属主 —— 逐个 token 试到能看见为止。
        for t in toks:
            s, d = W.api(f"/api/jobs/{jid}", t)
            j = (d or {}).get("job") or d or {}
            r = j.get("results") or {}
            ops = r.get("operators") or []
            if ops and (ops[0].get("cases") or ops[0].get("case_results")):
                cases = ops[0].get("cases") or ops[0].get("case_results")
                break
        if not cases:
            print(f"  [{n}/{len(comps)}] {key:<40} ✗ 拿不到逐 case")
            continue
        keep = []
        for x in cases:
            B, H, C = x.get("baseline_perf_us"), x.get("t_hw_us"), x.get("elapsed_us")
            ok = (x.get("status") == "success" and isinstance(C, (int, float)) and C > 0)
            if not all(isinstance(v, (int, float)) for v in (B, H)):
                continue
            keep.append([B, H, C if ok else None])
        out[key] = {"score": c["score"], "job": jid, "cases": keep}
        print(f"  [{n}/{len(comps)}] {key:<40} {len(keep)} cases")
    CACHE.write_text(json.dumps(out))
    return out


def perf(cases, k=1.0):
    """这一行的 perf 分（满分 50）。跑不出成绩的 case 记 0。"""
    if not cases:
        return 0.0
    tot = 0.0
    for B, H, C in cases:
        if C is None or B <= H:
            continue
        c = max(C / k, H)                       # C 不可能低于硬件下界 H
        tot += (B - H) / ((c - H) + (B - H))
    return 50.0 * tot / len(cases)


def main():
    args = [a for a in sys.argv[1:] if a != "--refresh"]
    ks = [float(a) for a in args] or KS
    if "--refresh" in sys.argv or not CACHE.exists():
        rows = fetch()
    else:
        rows = json.loads(CACHE.read_text())

    res = []
    for key, v in rows.items():
        base = perf(v["cases"])
        gains = [perf(v["cases"], k) - base for k in ks]
        res.append((gains[-1], key, v["score"], base, gains))
    res.sort(reverse=True)

    hdr = "".join(f"{'+'+str(k)+'x':>8}" for k in ks)
    print(f"\n{'行':<40}{'现分':>7}{'perf/50':>9}{hdr}")
    for _, key, sc, base, gains in res[:28]:
        print(f"{key:<40}{sc:7.1f}{base:9.1f}" + "".join(f"{g:8.1f}" for g in gains))
    for i, k in enumerate(ks):
        print(f"\n全部 {len(res)} 行 @ {k}x 合计 = {sum(r[4][i] for r in res):.0f}", end="")
    print(f"    （距 7200 需 {7200 - sum(r[2] for r in res):.0f}）")


if __name__ == "__main__":
    main()
