#!/usr/bin/env python3
"""把 910c **计分中**的源码拉下来，落到 livesrc910/<op>/。零 credit（只下载）。

为什么必须做这一步（2026-08-02 血的教训）：
`cann_submit/candidates/conv_3d_backprop_filter` 在本地真机 **0/20 correctness failed**，
而榜上同名行是 20/20 —— 说明那棵树**不是计分源码**。拿它做 A/B 等于在测一个
和榜面无关的东西。FINDINGS-012 §4.1 记过同样的事：四次 ForeachNorm 实验
白费在改一个计分树里根本不存在的常量上。

**任何实验开始前，先跑这个脚本。**

⚠ `/submission/download` 需要**属主账号**的 token，所以遍历 cann.env 里全部 token。

  pull910src.py [算子key ...]      # 不给参数则拉全部 106 行涉及的算子
"""
import os, sys, io, zipfile, pathlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wave as W

ENTRY = "sol_1b232feac1c6a392"          # SLAI_3 @ 910c, benchmark 1.1.0
OUT = pathlib.Path(os.path.dirname(os.path.abspath(__file__))) / "livesrc910"
# 🔴 2026-08-03 修:原来是 `range(2, 16)`，**漏了 1 号账号 `GITCODE_TOKEN`**（没有后缀）。
#   后果是**假阴性**：属主是 1 号账号的算子（例如 cross_entropy_loss）会报
#   "✗ 所有 token 都下不到"，让人以为拿不到 live 源码，从而对着陈旧副本做 A/B。
#   `/submission/download` **只认属主 token**，漏一个账号就等于漏掉它名下所有的行。
#   ⚠ 加账号时这里、`wave.py` 的 ACCTS、`credits_total.py` 的 ACC **三处都要改**。
TOKS = ([("GITCODE_TOKEN", os.environ["GITCODE_TOKEN"])] if os.environ.get("GITCODE_TOKEN") else []) + \
       [(f"GITCODE_TOKEN_{i}", os.environ[f"GITCODE_TOKEN_{i}"])
        for i in range(2, 20) if os.environ.get(f"GITCODE_TOKEN_{i}")]


def main():
    # ⚠ 榜上的 operator_key **没有下划线**（是 foreachnorm 不是 foreach_norm），
    #   而归档里的目录名有。两边都归一化后再比。
    norm = lambda s: s.replace("_", "").lower()
    want = {norm(a) for a in sys.argv[1:]}
    s, d = W.api(f"/api/leaderboard/solution/{ENTRY}/components", TOKS[0][1])
    comps = d.get("components") or d.get("entries") or []
    # 每个算子只需要拉一次（standard 优先）
    seen = {}
    for c in comps:
        k = c["operator_key"]
        if want and norm(k) not in want:
            continue
        if k in seen and seen[k]["case_set"] == "standard":
            continue
        seen[k] = c

    OUT.mkdir(exist_ok=True)
    ok = bad = 0
    for k, c in sorted(seen.items()):
        jid = c["job_id"]
        blob = None
        for tv, tok in TOKS:
            sc, body = W.api(f"/api/jobs/{jid}/submission/download", tok, raw=True, timeout=240)
            if sc == 200 and body and len(body) > 500:
                blob = body
                break
        if not blob:
            print(f"  {k:<32} ✗ 所有 token 都下不到 (job {jid})"); bad += 1; continue
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
        except Exception as e:
            print(f"  {k:<32} ✗ 不是 zip: {type(e).__name__}"); bad += 1; continue
        # 归档里算子目录形如 csrc/ops/<snake>/...，而 k 是无下划线的榜上 key，
        # 所以按归一化匹配找出真正的目录名。
        dirs = {n.split("/ops/", 1)[1].split("/", 1)[0]
                for n in zf.namelist() if "/ops/" in n and n.split("/ops/", 1)[1].strip("/")}
        snake = next((x for x in dirs if norm(x) == norm(k)), None)
        # 🔴 2026-08-04 修:**目录名不保证等于 operator_key**。实测 mhc_sinkhorn 的
        #   现役目录叫 `mhc_v2`、engram_gate_fusion 叫 `egf_v2`（抽查 3 个算子中了 2 个）。
        #   原来这里直接报 "✗ 归档里没有匹配目录" ⇒ 会被读成"拿不到 live 源码"，
        #   于是对着陈旧副本做 A/B —— 正是这个脚本存在的理由本身。
        #   归档里 csrc/ops/ 下只有该算子一个目录（外加一个 CMakeLists.txt），
        #   所以名字对不上时按"唯一的那个目录"兜底。**只在原本就会失败的分支里生效**，
        #   不可能改变任何本来成功的拉取。
        if snake is None:
            cand = sorted(d for d in dirs if not d.endswith(".txt"))
            if len(cand) == 1:
                snake = cand[0]
                print(f"  {k:<32} ⚠ 目录名是 {snake!r} 而非 {k!r}，按唯一目录兜底")
        names = [n for n in zf.namelist()
                 if snake and f"/ops/{snake}/" in n and not n.endswith("/")]
        if not names:
            print(f"  {k:<32} ✗ 归档里没有匹配目录 (有 {sorted(dirs)[:4]})"); bad += 1; continue
        dst = OUT / snake
        if dst.exists():
            import shutil; shutil.rmtree(dst)
        for n in names:
            rel = n.split(f"/ops/{snake}/", 1)[1]
            p = dst / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(zf.read(n))
        kf = list(dst.rglob("*_kernel.cpp"))
        sz = kf[0].stat().st_size if kf else 0
        print(f"  {k:<32} ✓ {len(names)} 文件  kernel {sz} 字节  (job {jid})")
        ok += 1
    print(f"\n落盘 {OUT}: 成功 {ok}, 失败 {bad}")


if __name__ == "__main__":
    main()
