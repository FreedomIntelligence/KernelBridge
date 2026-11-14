#!/usr/bin/env python3
'Compare source identities for completed standard and hidden evaluation jobs.'
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
