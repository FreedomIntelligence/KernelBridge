#!/usr/bin/env python3
"""910c 榜的逐行缺口（零 credit，只读榜）。

⚠ gap.py 默认读 950pr 榜且硬编码过滤 `hardware=='950pr'` ⇒ 在 910c 上会说"榜上没找到"。
本脚本按 910c + benchmark 1.1.0 过滤。
"""
import os, sys, json, collections
import wave as W

TOK = os.environ["GITCODE_TOKEN_5"]
US = "SLAI_5"
HW = "910c"
VER = "1.1.0"


def ops_of(entry):
    out = {}
    for o in entry.get("metric", {}).get("operators", []) or []:
        out[(o.get("operator") or "?").lower()] = {
            "score": o.get("score") or 0.0,
            "passed": o.get("passed_cases") or 0,
            "total": o.get("total_cases") or 0,
            "perf": o.get("performance_score") or 0.0,
            "func": o.get("function_score") or 0.0,
            "spd": o.get("avg_speedup"),
            "level": o.get("level"),
            "name": o.get("operator"),
        }
    return out


s, d = W.api("/api/leaderboard?mode=solution", TOK)
e = [x for x in d["entries"]
     if x.get("metric", {}).get("hardware") == HW and x.get("benchmark_version") == VER]
e.sort(key=lambda x: -float(x.get("score") or 0))

mine = next((x for x in e if x.get("display_user") == US), None)
if mine is None:
    print(f"{HW} v{VER} 榜上没找到 {US}", file=sys.stderr); sys.exit(1)
lead = e[0] if e[0].get("display_user") != US else e[1]

print(f"=== {HW} @ benchmark {VER} 前 6 ===")
for i, x in enumerate(e[:6], 1):
    mark = "  <= 我们" if x.get("display_user") == US else ""
    print(f"  {i}. {float(x['score']):9.2f}  {x.get('display_user'):<24}{mark}")
gap = float(lead["score"]) - float(mine["score"])
print(f"\n我们 {float(mine['score']):.2f}   榜首 {lead.get('display_user')} "
      f"{float(lead['score']):.2f}   **差 {gap:.2f}**")

us, them = ops_of(mine), ops_of(lead)
rows = [(k, us.get(k), them.get(k),
         (them.get(k, {}).get("score") or 0) - (us.get(k, {}).get("score") or 0))
        for k in sorted(set(us) | set(them))]
behind = [r for r in rows if r[3] > 0]
print(f"\n落后 {len(behind)} 行合计 {sum(r[3] for r in behind):.2f}；"
      f"领先 {len(rows)-len(behind)} 行合计 {sum(r[3] for r in rows if r[3]<0):.2f}；"
      f"净 {sum(r[3] for r in rows):.2f}（对照总分差 {gap:.2f}）")

print(f"\n=== 落后最多的 24 行 ===")
print(f"{'算子':<32}{'L':>3}{'我们':>8}{'榜首':>8}{'缺口':>8}{'我方pass':>10}{'我方spd':>9}")
for k, a, b, dd in sorted(behind, key=lambda x: -x[3])[:24]:
    nm = (b or a)["name"]
    passed = f"{a['passed']}/{a['total']}" if a else "-"
    speed = f"{a['spd']:.2f}x" if a and a.get("spd") is not None else "-"
    print(f"{nm:<32}{str((b or a).get('level') or '?'):>3}"
          f"{(a['score'] if a else 0):8.1f}{(b['score'] if b else 0):8.1f}{dd:8.2f}"
          f"{passed:>10}{speed:>9}")

json.dump({r[0]: {"ours": (r[1] or {}).get("score"), "lead": (r[2] or {}).get("score"),
                  "gap": r[3], "passed": (r[1] or {}).get("passed"),
                  "total": (r[1] or {}).get("total"), "spd": (r[1] or {}).get("spd")}
           for r in rows},
          open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gap910c_now.json"), "w"),
          ensure_ascii=False, indent=0)
print(f"\n已写 .gap910c_now.json（{len(rows)} 行）")
