#!/usr/bin/env python3
'Inspect source-bound completed-job correctness and timing coverage.\n\nRequire terminal state and owner-readable per-case results before comparing\ncorrectness and performance. A passed case without timing is incomplete evidence.'
import os, sys, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CANN_BOARD", "910c")
import wave as W


def load(jid):
    '找一个能看到 `results` 的 token。'
    fallback = None
    last = None
    for tv in W.ACCTS:
        if not os.environ.get(tv):
            continue
        try:
            m = W.job_meta(jid, os.environ[tv])
        except Exception as e:
            last = e; continue
        if not (m and m.get("status")):
            continue
        ops = (m.get("results") or {}).get("operators") or []
        if ops and (ops[0].get("cases") or []):
            return m, tv                     # 属主：能看到逐 case
        fallback = fallback or (m, tv)       # 非属主：只有 status
    if fallback:
        print(f"  ⚠ {jid}: 没有任何 token 能读到 results（逐 case） —— "
              f"只能报 status 级信息。属主 token 可能不在 cann.env 里。")
        return fallback
    raise SystemExit(f"读不到 {jid}（最后错误 {last}）")


def summarize(jid):
    m, tv = load(jid)
    r = m.get("results") or {}
    ops = r.get("operators") or []
    op = ops[0] if ops else {}
    cases = op.get("cases") or []
    timed = [c for c in cases if isinstance(c.get("elapsed_us"), (int, float)) and c["elapsed_us"] > 0]
    fails = [c for c in cases if not (c.get("accuracy") or {}).get("passed")]
    # 🔴 分母不能用 total —— 精度失败的 case **本来就没有计时**（它进不到 perf 阶段）。
    #    第一版把对照那发的 74/80 也标成"不全"并归因成平台繁忙，实际那 6 个是
    #    correctness 失败（59/65/66/75/77/97），无计时是**正常**的。
    #    真正的判据是：**通过的 case 是否都有计时**。
    passed_ids = {int(c["case_id"].split("_")[-1]) for c in cases
                  if (c.get("accuracy") or {}).get("passed")}
    timed_ids = {int(c["case_id"].split("_")[-1]) for c in timed}
    lost = sorted(passed_ids - timed_ids)
    return dict(job=jid, tok=tv, status=m.get("status"), case_set=m.get("case_set"),
                runner=m.get("runner_name"), device=r.get("device"),
                passed=m.get("passed_cases"), total=m.get("total_cases"),
                score=m.get("result_score"),
                compile=op.get("compilation_score"), fn=op.get("function_score"),
                perf=op.get("performance_score"),
                n_timed=len(timed), n_cases=len(cases),
                n_pass=len(passed_ids), lost=lost,
                failset=sorted(int(c["case_id"].split("_")[-1]) for c in fails),
                cases={int(c["case_id"].split("_")[-1]): c.get("elapsed_us") for c in cases})


def show(s):
    print(f"  job        {s['job']}   ({s['case_set']}, {s['status']})")
    print(f"  runner     {s['runner']}   device={s['device']}")
    print(f"  pass       {s['passed']}/{s['total']}      失败集 {s['failset']}")
    print(f"  计时       {s['n_timed']}/{s['n_cases']}"
          f"   通过的 {s['n_pass']} 个里丢计时 {len(s['lost'])} 个"
          + ("   ✅" if not s['lost'] else f"   🔴 {s['lost'][:12]}"))
    sc = s['score']
    print(f"  分数       score={sc if sc is None else round(sc,4)}  "
          f"compile={s['compile']}  fn={s['fn']}  perf={None if s['perf'] is None else round(s['perf'],4)}")


def main():
    if len(sys.argv) < 2:
        print(__doc__); return 2
    a = summarize(sys.argv[1])
    print("=" * 78); print("本发"); show(a)
    b = None
    if len(sys.argv) > 2:
        b = summarize(sys.argv[2])
        print(); print("对照"); show(b)

    print()
    print("=" * 78)
    print("判据（按顺序）")
    # 🔴 未终态的 job 没有 cases，不能判成"作废" —— 那是"还没跑完"，两回事。
    TERM = ('succeeded', 'failed', 'cancelled', 'correctness_failed',
            'compile_failed', 'hard_failure', 'case_failures')
    if not a['n_cases']:
        print(f"  ⏳ job 仍在 `{a['status']}` 阶段，还没有逐 case 数据 ⇒ **等它终态再判**"
              f"{'（状态已终态但无 results ⇒ 可能不是属主 token）' if a['status'] in TERM else ''}")
        return 2
    ok1 = not a['lost']
    print(f"  {'✅' if ok1 else '🔴'} 1. 通过的 {a['n_pass']} 个 case 里丢计时 {len(a['lost'])} 个"
          + ("" if ok1 else "  ⇒ **该发作废，重跑（rerun-hidden 或重交），不要解读分数**"))
    if not ok1:
        print(f"       丢计时的 case: {a['lost'][:20]}{' …' if len(a['lost'])>20 else ''}")
        print(f"       它们 speedup=0 ⇒ perf 被系统性压低；status 通常仍是 success、"
              f"error_msg 为 None（msprof 30s 超时）。")
        if b and b['n_cases']:
            print(f"       对照那发通过 {b['n_pass']} 个、丢计时 {len(b['lost'])} 个 ⇒ "
                  + ("同样丢，可能是平台整体繁忙" if b['lost']
                     else "**干净** ⇒ 是本发的 runner 撞上负载，不是代码问题"))

    if b:
        new = sorted(set(a['failset']) - set(b['failset']))
        gone = sorted(set(b['failset']) - set(a['failset']))
        ok2 = not new
        print(f"  {'✅' if ok2 else '🔴'} 2. 新增失败 {new or '无'}   （减少的失败 {gone or '无'}）")
        # 只在两发都有计时的 case 上比 C
        both = [(k, b['cases'][k], a['cases'][k]) for k in sorted(set(a['cases']) & set(b['cases']))
                if a['cases'].get(k) and b['cases'].get(k)]
        if both:
            rs = [x[2] / x[1] for x in both]
            rs.sort()
            med = rs[len(rs) // 2]
            off = [(k, round(v2 / v1, 3)) for k, v1, v2 in both if abs(v2 / v1 - 1) > 0.05]
            print(f"  {'✅' if not off else '⚠'} 3. 共有计时的 {len(both)} 个 case：C 比值 中位 {med:.4f}，"
                  f"偏离 >5% 的 {off or '无'}")
            print(f"       ⇒ 这是**唯一**能在丢计时的发次里做的性能比较（只用两边都有的 case）。")

    print()
    if ok1:
        print("✅ 计时齐全 ⇒ 可以解读分数。")
    else:
        print("🔴 计时不全 ⇒ **先重跑**。榜面按半场取最优 ⇒ 重跑只可能变好或不变，不会掉分。")
        print("   ⭐ 识别信号：pass_rate 升了而 score 降了 ⇒ 只可能是丢计时（function 单调依赖 pass_rate）。")
    return 0 if ok1 else 1


if __name__ == '__main__':
    sys.exit(main())
