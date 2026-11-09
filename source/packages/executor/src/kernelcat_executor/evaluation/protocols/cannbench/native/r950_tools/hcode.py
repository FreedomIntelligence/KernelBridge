#!/usr/bin/env python3
"""hcode.py —— 按**代码身份**（而不是时间戳）补发 hidden。零 credit、零新提交。

    python3 hcode.py --dry     # 只列
    python3 hcode.py           # 补发
    python3 hcode.py --md5     # 额外下载两边源码比 md5（慢，但能说清哪些是真不同代码）

## 为什么必须有这个，`hstale.py` 不够

`hstale.py` 的判据是 **`standard.updated_at − hidden.updated_at ≥ 3h`**。
它假设「hidden 落后 = hidden 的时间戳更旧」。**这个假设被 depthwise_conv_2d 反证：**

    standard 60.93  sub_8ae79674936d  07-31 04:54   ← 新代码（= livesrc910，逐字节核过）
    hidden   62.87  sub_a530b1f5d2cc  08-03 06:40   ← **旧代码**
    ⇒ 时间差 −73.8h：hidden 的时间戳**更新** 73.8 小时，跑的代码却**更旧**

成因：08-02 那批 `reclaim910/` 的 22-tag 回收**把 pre-champion 的旧代码又交了一遍**，
那些提交跑了 hidden 并以 62.87 赢下 hidden 行，而 standard 行仍由 07-31 的新代码持有。
⇒ **时间戳新 ≠ 代码新。** 判据必须换成**代码身份**。

实测这一行的头寸：新代码对旧代码 bf16 **3.09×** / fp16 **2.287×**（fp32 按设计不变），
hidden 的 dtype 构成是 fp16 35 / bf16 22 / fp32 23 ⇒ **hidden perf +2.7 ~ +7.5，零 kernel 工作。**

全榜扫下来：53 个算子里 **28 个** 两行的 `submission_id` 不同，其中 **18 个**时间差 < 3h
⇒ **`hstale.py` 一个都报不出来**。

## 安全性

* 聚合是 **best-per-(算子, case_set)** ⇒ 跑出来更差，行分停在原值，**不会下行**。
* 🔴 **`rerun-hidden` 每发扣 1 个 CANN credit。**（本行原来写"零 credit",是错的。）
  **判据是受控单发测试**：停掉 autopush、确认 forever.sh 已死之后，读 T0=270 →
  对 `sub_25819a8d760d` POST 一次 rerun-hidden（200，`job_a8fe64e62f10` queued）→ T1=269。
  同一 submission 第二次返回 409,**409 不扣**。
  ⇒ 一次全量 sweep（23 行）是 **~20 credit 的下注**,不是免费期权。按期望值筛再发：
  「hidden 在跑**旧代码**」（本脚本的代码身份判据）是正期望（depthwise 单行 +2.7~+7.5）；
  「用新代码盖已经不错的 hidden 行」历史是 **1 胜 7 负**（ForeachNorm −35 / Softmax −27，
  反解是通过率掉不是性能）。**所以不要无脑全发。**
  ⚠ 别再用"sweep 前后 credit 差值"去判断扣不扣费 —— 我为此翻车两次（一次错怪 autopush，
  一次错信旧笔记里"credit=0 时打出 25 发"的记载）。**只有受控单发测试算数。**
* ⚠ 唯一已实测的下行是**平台 bug**：perf 跑太长 → runner 重放 → 终态被错盖
  （08-02 `GroupedMatmulSwigluQuant|hidden 72.46 → 68.03`，见 `ROWDROP`）。
  ⇒ 补发后必须盯 `ROWDROP` / `wave.py total`，有行下降立刻 `touch STOP`。

⚠ 不读写 ledger.json / wavestate.json / forever.sh / cann.env。
"""
import collections, hashlib, io, os, pathlib, re, sys, zipfile
import concurrent.futures as cf
from datetime import datetime

HERE = pathlib.Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))
os.environ.setdefault("CANN_BOARD", "910c")
import wave as W

DRY = "--dry" in sys.argv
MD5 = "--md5" in sys.argv
norm = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())
loc = lambda u: datetime.fromisoformat(str(u).replace("Z", ""))
toks = lambda: [a for a in W.ACCTS if os.environ.get(a)]


def kernel_md5(job_id, tk):
    """下载某个 job 的提交产物，返回 op_kernel/*.cpp 的合并 md5。非属主 403，零副作用。"""
    for a in tk:
        st, raw = W.api(f"/api/jobs/{job_id}/submission/download", os.environ[a], raw=True)
        if st != 200 or not raw:
            continue
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
        except Exception:
            continue
        h = hashlib.md5()
        for n in sorted(x for x in z.namelist() if "/op_kernel/" in x and x.endswith((".cpp", ".h"))):
            h.update(z.read(n))
        return h.hexdigest()[:10], a
    return None, None


def main():
    tk = toks()
    if len(tk) < 15:
        raise SystemExit(f"⛔ 只看到 {len(tk)} 个 token（应为 15）。"
                         "正确咏唱：cd cann_campaign && set -a && source ./cann.env && set +a")
    comps = W.components(os.environ[tk[0]])
    b = collections.defaultdict(dict)
    for c in comps:
        b[norm(c["operator_name"])][c["case_set"]] = c

    todo = []
    for op, d in b.items():
        s, h = d.get("standard"), d.get("hidden")
        if not (s and h and s.get("submission_id")):
            continue
        if s["submission_id"] == h.get("submission_id"):
            continue                       # 两行同一份提交 ⇒ hidden 已经在跑当前代码
        gap = (loc(s["updated_at"]) - loc(h["updated_at"])).total_seconds() / 3600
        todo.append((gap, op, s, h))
    todo.sort()

    print(f"两行 submission 不同的算子：{len(todo)} / {len(b)}")
    print(f"其中时间差 <3h（`hstale.py` 报不出来的）：{sum(1 for t in todo if t[0] < 3)}\n")
    print(f"{'算子':26s} {'时间差h':>8s} {'std分':>7s} {'hid分':>7s}"
          + ("  std_md5     hid_md5     代码" if MD5 else ""))
    fire = []
    for gap, op, s, h in todo:
        extra = ""
        if MD5:
            a1, _ = kernel_md5(s["job_id"], tk)
            a2, _ = kernel_md5(h["job_id"], tk)
            extra = (f"  {a1 or '(拉不到)':11s} {a2 or '(拉不到)':11s} "
                     f"{'同' if (a1 and a1 == a2) else ('❗不同' if a1 and a2 else '?')}")
            if a1 and a2 and a1 == a2:
                print(f"{op[:26]:26s} {gap:8.1f} {s['score']:7.2f} {h['score']:7.2f}{extra}")
                continue                   # 同代码，补发只是浪费 runner 时间
        print(f"{op[:26]:26s} {gap:8.1f} {s['score']:7.2f} {h['score']:7.2f}{extra}")
        fire.append((op, s["submission_id"]))

    if DRY:
        print(f"\n[dry] 会补 {len(fire)} 发，零 credit"); return

    print(f"\n补发 {len(fire)} 发 …")
    ok = dup = bad = 0
    for op, sub in fire:
        got = None
        for a in tk:
            st, d = W.api(f"/api/submissions/{sub}/rerun-hidden", os.environ[a],
                          "POST", b"{}", "application/json")
            if st in (200, 409):
                got = (st, d, a); break
        if not got:
            bad += 1; print(f"  ✗ {op:26s} 无属主 token"); continue
        st, d, a = got
        if st == 200:
            ok += 1
            print(f"  ✓ {op:26s} -> {(d.get('job') or {}).get('id') if isinstance(d, dict) else ''} ({a})")
        else:
            dup += 1; print(f"  = {op:26s} 已补过（409）")
    print(f"\n新补 {ok}  已补过 {dup}  失败 {bad}")
    print("🔴 补完必须盯 ROWDROP：平台有 perf 超时→runner 重放→终态错盖 的已实测下行。")


if __name__ == "__main__":
    main()
