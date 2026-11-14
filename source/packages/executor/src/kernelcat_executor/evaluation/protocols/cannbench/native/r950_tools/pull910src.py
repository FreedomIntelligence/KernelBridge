#!/usr/bin/env python3
'Download the configured solution source with its owner token. Set CANN_SOLUTION_ID.'
import os, sys, io, zipfile, pathlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wave as W

ENTRY = os.environ.get("CANN_SOLUTION_ID", "")
OUT = pathlib.Path(os.path.dirname(os.path.abspath(__file__))) / "livesrc910"
TOKS = ([("GITCODE_TOKEN", os.environ["GITCODE_TOKEN"])] if os.environ.get("GITCODE_TOKEN") else []) + \
       [(f"GITCODE_TOKEN_{i}", os.environ[f"GITCODE_TOKEN_{i}"])
        for i in range(2, 20) if os.environ.get(f"GITCODE_TOKEN_{i}")]


def main():
    if not ENTRY or not TOKS:
        raise ValueError("CANN_SOLUTION_ID and an owner GITCODE_TOKEN are required")
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
