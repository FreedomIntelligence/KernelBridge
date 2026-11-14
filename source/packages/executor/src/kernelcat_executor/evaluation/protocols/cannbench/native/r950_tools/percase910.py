#!/usr/bin/env python3
'Read source-bound per-case timings and score contributions from completed jobs.\n\nFilter by benchmark and hardware. Only the job owner can read full results;\nmissing timing or incomplete correctness must not count as performance evidence.'
import os, sys, json, re, pathlib, collections
import concurrent.futures as cf

HERE = pathlib.Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))
os.environ.setdefault("CANN_BOARD", "910c")
import wave as W

CACHE = HERE / ".percase910.json"
SLUG, VER, HW = "official-tasks", "1.1.0", "910c"


def toks():
    return [a for a in W.ACCTS if os.environ.get(a)]


def collect():
    """拉榜面当前 106 个 component job 的逐 case 明细。

    不能再从每个账号的 ``/api/jobs?limit=200`` 反推当前行：活跃账号会把仍在
    BEST-of 中计分的老 job 挤出窗口。``solution/<id>/components`` 才是当前榜面的
    权威索引，并且同时给出 component job 的 owner，足以选对可见 results 的 token。
    """
    if not W.SOL:
        raise ValueError("Set CANN_SOLUTION_ID before collecting solution results")
    tk = toks()
    if not tk:
        raise RuntimeError("没有可用 GITCODE_TOKEN；请先 source cann_campaign/cann.env")

    manifest = None
    for a in tk:
        s, d = W.api(f"/api/leaderboard/solution/{W.SOL}/components", os.environ[a])
        if s == 200 and isinstance(d, dict) and d.get("components"):
            manifest = d
            break
    if manifest is None:
        raise RuntimeError("拿不到 solution components；无法证明逐案缓存对应当前榜面")

    owner_path = HERE / ".owner_map.json"
    try:
        owner_map = json.loads(owner_path.read_text())
    except Exception:
        owner_map = {}
    job_owner = {j.get("job_id"): owner_map.get(j.get("user_id"))
                 for j in (manifest.get("component_jobs") or [])}
    best = {}                       # (算子, case_set) -> (榜面行分, jid, owner账号, total_cases)
    for c in manifest.get("components") or []:
        op = c.get("operator_name") or c.get("operator_key")
        k = (re.sub(r"[^a-z0-9]", "", str(op).lower()), c.get("case_set"))
        best[k] = (c.get("score") or 0.0, c.get("job_id"), job_owner.get(c.get("job_id")),
                   c.get("total_cases"))
    print(f"榜面 component {len(best)} 个 (算子, case_set)，拉逐 case 明细 …", file=sys.stderr)

    def one(item):
        (opn, cs), (sc, jid, owner, n_all) = item
        # results 只对属主 token 可见。owner map 正常时一次命中；映射陈旧时才依次
        # 尝试其余账号，避免把“metadata 可见但 results 为空”当成真实空结果。
        order = ([owner] if owner in tk else []) + [a for a in tk if a != owner]
        m = {}
        for a in order:
            candidate = W.job_meta(jid, os.environ[a]) or {}
            if ((candidate.get("results") or {}).get("operators") or []):
                m = candidate
                break
            # UID→账号映射来自同一 component manifest。属主能读到 job 但老 job 已无
            # results 时，换 14 个账号不会复活数据，只会制造 API 风暴。
            if a == owner and candidate:
                m = candidate
                break
        out = []
        for o in ((m.get("results") or {}).get("operators") or []):
            if re.sub(r"[^a-z0-9]", "", str(o.get("operator")).lower()) != opn:
                continue
            for c in (o.get("cases") or []):
                if c.get("status") != "success":
                    continue
                B, C, H = c.get("baseline_perf_us"), c.get("elapsed_us"), c.get("t_hw_us")
                if not (B and C and H):
                    continue
                out.append({
                    "op": o.get("operator"), "case_set": cs, "case_id": c.get("case_id"),
                    "B": B, "C": C, "H": H, "s": c.get("perf_score"),
                    "speedup": c.get("speedup"), "job": jid,
                    "n_all": n_all,
                    "kernels": list((c.get("op_times") or {}).get("device_kernels") or {}),
                })
        return out
    rows = []
    with cf.ThreadPoolExecutor(10) as ex:
        for r in ex.map(one, best.items()):
            rows += r
    return rows


def load(refresh=False):
    if CACHE.exists() and not refresh:
        return json.load(open(CACHE))
    old = json.load(open(CACHE)) if CACHE.exists() else []
    rows = collect()
    got = {(re.sub(r"[^a-z0-9]", "", str(r["op"]).lower()), r["case_set"])
           for r in rows}
    # 极老 component 的 owner-visible metadata 也可能已不保留 results。刷新不能因此
    # 反向缩表；仅在整个 (op,case_set) 都取不到时保留旧行，并明确报警。
    retained = [r for r in old
                if (re.sub(r"[^a-z0-9]", "", str(r["op"]).lower()), r["case_set"]) not in got]
    if retained:
        kept = {(re.sub(r"[^a-z0-9]", "", str(r["op"]).lower()), r["case_set"])
                for r in retained}
        print(f"⚠ {len(kept)} 个 component 无 owner-visible results，保留旧缓存："
              f"{sorted(kept)}", file=sys.stderr)
        rows += retained
    json.dump(rows, open(CACHE, "w"))
    return rows


def s_of(B, C, H):
    """平台的逐 case perf 分。C→H 时趋于 1，C→∞ 时趋于 0。B==H 时退化为满分。"""
    d = (C - H) + (B - H)
    return 1.0 if d <= 0 else (B - H) / d


N_ALL = {}          # (算子小写, 用例集) -> total_cases（榜面权威值）


def n_all_of(op, cs, fallback):
    return N_ALL.get((re.sub(r"[^a-z0-9]", "", str(op).lower()), cs), fallback)


def load_n_all():
    """从榜面拉每行的 total_cases —— 权重分母的唯一权威来源。"""
    if N_ALL:
        return
    try:
        T = os.environ[toks()[0]]
        _, d = W.api("/api/leaderboard?mode=solution&benchmark_slug=official-tasks"
                     "&benchmark_version=1.1.0&limit=100", T)
        for e in (d or {}).get("entries", []):
            if (e.get("metric") or {}).get("hardware") != HW:
                continue
            for key, cs in (("metric", "standard"), ("hidden_metric", "hidden")):
                for o in ((e.get(key) or {}).get("operators") or []):
                    n = o.get("total_cases")
                    if n:
                        N_ALL[(re.sub(r"[^a-z0-9]", "", str(o.get("operator")).lower()), cs)] = n
    except Exception as ex:
        print(f"⚠ 拉 total_cases 失败（天花板可能偏高）: {ex}", file=sys.stderr)


def oracle():
    """Compare each configured-account row against complete same-hardware results."""
    US = os.environ.get("CANN_US", "")
    if not US:
        raise ValueError("Set CANN_US before comparing account results")
    tk = toks()
    if not tk:
        raise ValueError("Set an owner GITCODE_TOKEN before comparing account results")
    T = os.environ[tk[0]]
    s, d = W.api("/api/leaderboard?mode=solution&benchmark_slug=official-tasks"
                 "&benchmark_version=1.1.0&limit=100", T)
    ent = [e for e in (d or {}).get("entries", [])
           if (e.get("metric") or {}).get("hardware") == HW
           and e.get("benchmark_version") == VER
           and e.get("leaderboard_mode") == "solution"]
    nm = lambda x: re.sub(r"[^a-z0-9]", "", str(x).lower())
    mine, best = {}, {}
    for e in ent:
        who = str(e.get("display_user"))
        for key, cs in (("metric", "standard"), ("hidden_metric", "hidden")):
            for o in ((e.get(key) or {}).get("operators") or []):
                v = o.get("score")
                if v is None:
                    continue
                v = v / 50.0     # 归一到与旧口径同量级（下游一律 ×50 还原成分数）
                k = (nm(o.get("operator")), cs)
                if who == US:
                    mine[k] = max(mine.get(k, -1), v)
                if k not in best or v > best[k][0]:
                    best[k] = (v, who)
    if not ent:
        raise SystemExit(f"❌ oracle(): 过滤后 0 条 entry —— 检查 metric.hardware=={HW} 是否还对")
    if not mine:
        raise SystemExit(f"❌ oracle(): 找不到我们自己（US={US}）的行。"
                         f"榜上有：{sorted({str(e.get('display_user')) for e in ent})[:8]}\n"
                         f"   用 CANN_US=<用户名> 覆盖。**不要让它静默返回空表。**")
    # C/H 可行性刻度（有逐 case 数据的行才有）
    ch = {}
    for r in load():
        ch.setdefault((nm(r["op"]), r["case_set"]), []).append(r["C"] / r["H"])
    gap = sorted(((50 * (best[k][0] - v), k[0], k[1], 50 * v, 50 * best[k][0], best[k][1])
                  for k, v in mine.items() if k in best and best[k][0] > v), reverse=True)
    print(f"有人证明过更好的行：{len(gap)} / {len(mine)}   合计可追 {sum(g[0] for g in gap):+.1f} 分")
    print(f"{'算子':26s} {'集':9s} {'我们':>7s} {'最好':>7s} {'差':>7s} {'C/H':>8s}  被谁")
    for dd, op, cs, a, b, who in gap:
        v = ch.get((op, cs))
        m = sorted(v)[len(v) // 2] if v else None
        print(f"{op[:26]:26s} {cs:9s} {a:7.2f} {b:7.2f} {dd:+7.2f} "
              f"{(f'{m:8.1f}' if m else '       —')}  {who}")
    moat = [k for k in mine if k not in best or best[k][0] <= mine[k]]
    print(f"\n无人超过我们的行（护城河）：{len(moat)}")


def main():
    a = sys.argv[1:]
    if "--oracle" in a:
        oracle(); return
    rows = load("--refresh" in a)
    if not rows:
        print("没有可用的逐 case 数据"); return
    load_n_all()      # 必须在任何加权之前 —— 分母是 total_cases，见 N_ALL 上方注释
    if "--grid" in a:
        import statistics as _st
        g = collections.defaultdict(list)
        for r in rows:
            g[(r["op"], r["case_set"])].append(r)
        load_n_all()
        def val(v, k):
            w = 50.0 / n_all_of(v[0]["op"], v[0]["case_set"], len(v))
            return sum(w * (s_of(x["B"], max(x["C"] / k, x["H"]), x["H"]) - x["s"]) for x in v)
        o = sorted(((val(v, 1.3), op, cs, sum(x["s"] for x in v) / len(v) * 50,
                     _st.median(x["C"] / x["H"] for x in v),
                     val(v, 2), val(v, 5), val(v, 20), val(v, 100))
                    for (op, cs), v in g.items()), reverse=True)
        print(f"{'算子':26s} {'集':9s} {'现perf':>6s} {'C/H':>9s} | "
              f"{'1.3×':>6s} {'2×':>6s} {'5×':>6s} {'20×':>6s} {'100×':>6s}")
        for t13, op, cs, cur, c_h, t2, t5, t20, t100 in o:
            print(f"{op[:26]:26s} {cs:9s} {cur:6.2f} {c_h:9.1f} | "
                  f"{t13:+6.2f} {t2:+6.2f} {t5:+6.2f} {t20:+6.2f} {t100:+6.2f}")
        for nm_, i in (("1.3×", 0), ("2×", 5), ("5×", 6), ("20×", 7), ("100×", 8)):
            print(f"  全 {len(o)} 行 {nm_:5s} 合计 {sum(x[i] for x in o):+8.1f}")
        return
    only = None
    if "--op" in a:
        only = re.sub(r"[^a-z0-9]", "", a[a.index("--op") + 1].lower())

    grp = collections.defaultdict(list)
    for r in rows:
        grp[(r["op"], r["case_set"])].append(r)

    # 每 case 的可回收分：把它做到 roofline 值多少 / 只提速 2× 值多少
    recs = []
    for (op, cs), cases in grp.items():
        if only and re.sub(r"[^a-z0-9]", "", op.lower()) != only:
            continue
        n = n_all_of(op, cs, len(cases))
        w = 50.0 / n
        for c in cases:
            B, C, H, s = c["B"], c["C"], c["H"], c["s"]
            recs.append({
                "op": op, "cs": cs, "case": c["case_id"], "n": n,
                "B": B, "C": C, "H": H, "s": s,
                "to_roof": w * (1 - s),                       # 理论上限
                "to_2x": w * (s_of(B, max(C / 2, H), H) - s),  # 现实档：提速 2×
                "us_gap": C - H,
            })

    if "--rows" in a:
        print(f"{'算子':26s} {'集':9s} {'n':>3s} {'perf分':>7s} {'上限':>7s} "
              f"{'最烂case占上限':>13s}  最烂的 case")
        agg = collections.defaultdict(list)
        for r in recs:
            agg[(r["op"], r["cs"])].append(r)
        out = []
        for (op, cs), v in agg.items():
            cur = sum(x["s"] for x in v) / n_all_of(op, cs, len(v)) * 50
            roof = sum(x["to_roof"] for x in v)
            worst = max(v, key=lambda x: x["to_roof"])
            out.append((roof, op, cs, len(v), cur, worst))
        for roof, op, cs, n, cur, worst in sorted(out, reverse=True):
            share = worst["to_roof"] / roof * 100 if roof else 0
            print(f"{op[:26]:26s} {cs:9s} {n:3d} {cur:7.2f} {roof:+7.2f} "
                  f"{share:12.0f}%  {worst['case'].split('/')[-1][:28]:28s} "
                  f"s={worst['s']:.3f} C={worst['C']:.0f}us H={worst['H']:.0f}us")
        return

    print(f"{'算子':24s} {'集':9s} {'case':30s} {'s_i':>6s} {'C(us)':>9s} {'H(us)':>8s} "
          f"{'到roofline':>10s} {'提速2×':>7s}")
    for r in sorted(recs, key=lambda x: -x["to_roof"])[:60]:
        print(f"{r['op'][:24]:24s} {r['cs']:9s} {r['case'].split('/')[-1][:30]:30s} "
              f"{r['s']:6.3f} {r['C']:9.1f} {r['H']:8.1f} {r['to_roof']:+10.2f} {r['to_2x']:+7.2f}")
    print(f"\n合计：到 roofline {sum(r['to_roof'] for r in recs):+.1f} 分，"
          f"全部提速 2× {sum(r['to_2x'] for r in recs):+.1f} 分（{len(recs)} 个 case）")


if __name__ == "__main__":
    main()
