import json, glob, os, sys

base = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ab_rb8"
rows = []
for d in sorted(glob.glob(base + "/rep_*")):
    fs = glob.glob(os.path.join(d, "cann_final_eval*.json"))
    if not fs:
        continue
    f = sorted(fs, key=os.path.getmtime)[-1]
    o = (json.load(open(f)).get("operators") or [{}])[0]
    cs = o.get("cases") or []
    timed = sum(1 for c in cs if isinstance(c.get("elapsed_us"), (int, float)) and c["elapsed_us"] > 0)
    rows.append((os.path.basename(d), timed, len(cs), o.get("passed_cases"),
                 o.get("total_cases"), o.get("score"), o.get("avg_speedup")))

print("%-14s %10s %10s %9s %8s" % ("rep", "计时", "过", "分", "sp"))
for n, t, tc, p, pc, sc, sp in rows:
    print("%-14s %10s %10s %9.2f %8.3f" % (n, f"{t}/{tc}", f"{p}/{pc}", sc or 0, sp or 0))

# 只看通过的 case 是否都有计时（分母 = 通过数，不是 total）
print()
for n, t, tc, p, pc, sc, sp in rows:
    lost = (p or 0) - t
    flag = "✅" if lost <= 0 else f"🔴 通过的里丢 {lost} 个计时"
    print("  %-14s 通过 %s，有计时 %s  %s" % (n, p, t, flag))
