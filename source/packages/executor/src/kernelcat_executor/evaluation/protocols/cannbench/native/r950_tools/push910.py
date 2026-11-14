#!/usr/bin/env python3
'Submit source packages with owner-bound receipts and optional promotion gates.\n\nUsage: push910.py OP:SOURCE[:NOTE] ...; --check-gate validates without submitting.\nExisting GATE.json is always validated. PUSH_REQUIRE_GATE=1 also requires it.'
import os, sys, json, time, subprocess, shutil, tempfile, pathlib, hashlib, re, zipfile
import concurrent.futures as cf

HERE = pathlib.Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))
os.environ.setdefault("CANN_BOARD", "910c")
import wave as W
from promotion_gate import validate_gate

TRAJ = HERE / "TRAJECTORY.jsonl"
SNAP = HERE / ".push910_snap.json"
CLAIM_DIR = HERE / ".push910_claims"

_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_TEXT_SUFFIX = {
    ".sh", ".py", ".cmake", ".txt", ".md", ".json", ".yaml", ".yml",
    ".cfg", ".in", ".hpp", ".h", ".cpp", ".c", ".cc", ".cu",
}
_TEXT_NAMES = {"CMakeLists.txt", "Makefile", "Dockerfile", "build.sh"}


def parse_spec(a: str) -> tuple[str, str, str]:
    """Parse ``op:src[:note]``. ``src`` may be a Windows ``D:\\...`` path."""
    if ":" not in a:
        raise ValueError(f"spec must be op:src[:note], got {a!r}")
    op, rest = a.split(":", 1)
    if not op or not rest:
        raise ValueError(f"spec must be op:src[:note], got {a!r}")
    if _WIN_DRIVE.match(rest):
        drive, body = rest[:2], rest[2:]
        if ":" in body:
            path_body, note = body.rsplit(":", 1)
            if "/" in note or "\\" in note:
                return op, rest, ""
            return op, drive + path_body, note
        return op, rest, ""
    if ":" in rest:
        src, note = rest.rsplit(":", 1)
        return op, src, note
    return op, rest, ""


def ops_dir_name(srcdir: str | pathlib.Path) -> str:
    """910c_tasks/<op>/arm packs as csrc/ops/<op>, not csrc/ops/arm."""
    p = pathlib.Path(srcdir)
    if p.name == "arm":
        return p.parent.name
    return p.name


def to_lf_tree(root: pathlib.Path) -> None:
    """Windows checkout is CRLF; the 910c runner's bash rejects $'\\r' in build.sh."""
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in _TEXT_SUFFIX and p.name not in _TEXT_NAMES:
            continue
        data = p.read_bytes()
        if b"\r" not in data:
            continue
        p.write_bytes(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))


def write_zip(pkg: pathlib.Path, zp: pathlib.Path) -> None:
    """Zip pkg contents as archive root. Avoid the Windows ``zip`` CLI."""
    with zipfile.ZipFile(zp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(pkg.rglob("*"), key=lambda x: x.as_posix()):
            if p.is_file():
                zf.write(p, p.relative_to(pkg).as_posix())


def source_fingerprint(src):
    """Stable content hash for one operator tree; paths/mtimes do not affect it."""
    root = pathlib.Path(src).resolve()
    h = hashlib.sha256()
    if not root.is_dir():
        h.update(b"MISSING\0" + str(root).encode())
        return h.hexdigest()
    for p in sorted(root.rglob("*"), key=lambda x: x.as_posix()):
        if not p.is_file() or any(x in {".git", "__pycache__"} for x in p.parts):
            continue
        rel = p.relative_to(root).as_posix()
        if p.name == ".DS_Store" or p.name.startswith("._"):
            continue
        h.update(rel.encode()); h.update(b"\0")
        h.update(p.read_bytes()); h.update(b"\0")
    return h.hexdigest()


def acquire_submit_claim(op, src):
    """Atomically dedupe concurrent/replayed official submits of identical source.

    A genuinely changed tree has a different content hash and is not blocked.  The
    explicit PUSH_FORCE_DUP=1 escape hatch is reserved for intentional same-source
    resubmission.  An owner that dies before recording a job leaves a reclaimable
    empty claim; successful-job claims remain durable campaign evidence.
    """
    fp = source_fingerprint(src)
    if os.environ.get("PUSH_FORCE_DUP") == "1":
        return None, fp, None
    CLAIM_DIR.mkdir(parents=True, exist_ok=True)
    safe_op = "".join(c if c.isalnum() or c in "-_" else "_" for c in op)
    path = CLAIM_DIR / f"{safe_op}__{fp}.json"
    payload = {
        "op": op, "src": str(pathlib.Path(src).resolve()), "source_sha256": fp,
        "pid": os.getpid(), "claimed_at": time.time(), "job": "", "submission": "",
    }
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                prior = json.loads(path.read_text())
            except Exception:
                return path, fp, {"job": "", "reason": "unreadable_claim"}
            pid = int(prior.get("pid") or 0)
            alive = False
            if pid > 0:
                if os.name == "nt":
                    # Windows: os.kill(pid, 0) maps to TerminateProcess on a live
                    # holder and can raise SystemError besides OSError, so probe
                    # liveness read-only via tasklist instead.
                    import subprocess as _sp
                    try:
                        _r = _sp.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                                     capture_output=True, text=True, timeout=10)
                        alive = str(pid) in (_r.stdout or "")
                    except Exception:
                        alive = True  # probe failed: assume alive, never re-push
                else:
                    try:
                        os.kill(pid, 0); alive = True
                    except OSError:
                        pass
            age = time.time() - float(prior.get("claimed_at") or 0)
            if not prior.get("job") and not alive and age > 300:
                try:
                    path.unlink()
                    continue
                except FileNotFoundError:
                    continue
            return path, fp, prior
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path, fp, None


def finish_submit_claim(path, **updates):
    if path is None:
        return
    try:
        data = json.loads(path.read_text())
    except Exception:
        data = {}
    data.update(updates)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def release_submit_claim(path):
    if path is not None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# ── 轨迹 ──────────────────────────────────────────────────────────────────
def rec(**kw):
    kw["t"] = time.strftime("%F %T")
    with open(TRAJ, "a") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\n")
    return kw


def board():
    """(总分, {(算子,用例集): 分}) —— 零 credit。TOKEN_5 is 401; try live tokens."""
    order = [
        "GITCODE_TOKEN_19", "GITCODE_TOKEN_18", "GITCODE_TOKEN_17", "GITCODE_TOKEN_16",
        "GITCODE_TOKEN_12", "GITCODE_TOKEN_10", "GITCODE_TOKEN_11",
        "GITCODE_TOKEN_13", "GITCODE_TOKEN_14", "GITCODE_TOKEN_9",
        "GITCODE_TOKEN", "GITCODE_TOKEN_15", "GITCODE_TOKEN_2",
        "GITCODE_TOKEN_5",
    ]
    last = None
    for a in order:
        tok = os.environ.get(a) or ""
        if not tok:
            continue
        cs = W.components(tok)
        if cs:
            return (
                sum(c["score"] for c in cs),
                {(c["operator_name"], c["case_set"]): c["score"] for c in cs},
            )
        last = a
    raise RuntimeError(f"board() no working token (last {last})")


def snapshot(tag=""):
    tot, rows = board()
    prev = json.load(open(SNAP)) if SNAP.exists() else None
    delta, moved = 0.0, []
    if prev:
        delta = tot - prev["total"]
        for k, v in rows.items():
            o = prev["rows"].get("|".join(k))
            if o is not None and abs(v - o) > 1e-6:
                moved.append((k[0], k[1], round(o, 2), round(v, 2), round(v - o, 2)))
    json.dump({"total": tot, "rows": {"|".join(k): v for k, v in rows.items()}},
              open(SNAP, "w"))
    return tot, delta, moved


# ── 账号轮换 ──────────────────────────────────────────────────────────────
def capacity():
    """[(账号, 剩余 credit, 空 job 槽)]，按剩余额度降序。"""
    def one(a):
        t = os.environ.get(a, "")
        if not t:
            return (a, 0, 0)
        s, d = W.api("/api/submissions/credits", t)
        rem = (d or {}).get("credits", {}).get("remaining", 0) if s == 200 else 0
        s2, d2 = W.api("/api/jobs?limit=50", t)
        act = sum(1 for j in (d2 or {}).get("jobs", []) if j.get("status") not in W.TERM) \
            if s2 == 200 else 3
        return (a, rem, max(0, 3 - act))
    with cf.ThreadPoolExecutor(max_workers=len(W.ACCTS)) as ex:
        cap = list(ex.map(one, W.ACCTS))
    # Credits, not the three-slot ceiling, are the scarce resource.  Sorting
    # only by min(credits, slots) made every healthy account tie at three and
    # silently concentrated every fresh process on the first token.
    order = {name: i for i, name in enumerate(W.ACCTS)}
    return sorted(cap, key=lambda x: (-x[1], -x[2], order[x[0]]))


def pick(cap):
    '挑一个既有 credit 又有空 job 槽、且**未被 gate 链预留**的账号，并就地扣减。'
    force = os.environ.get("PUSH_ACCT", "").strip()
    if force:
        print(f"  PUSH_ACCT 指定账号 {force}")
        return force if force in W.ACCTS else None
    skip = {x.strip() for x in os.environ.get("PUSH_SKIP_ACCTS", "").split(",") if x.strip()}
    try:
        reserved = W.reserved_accounts()
    except Exception:
        reserved = set()          # 读不出来就交给 final_submission_allowed 兜底
    for i, (a, rem, slots) in enumerate(cap):
        if a in reserved or a in skip:
            continue
        if rem > 0 and slots > 0:
            # Round-robin within this batch.  Without moving the chosen entry,
            # a 2–3 arm fast-submit batch consumed all three slots (and later
            # hidden credits) of one account before touching the other 18.
            cap.pop(i)
            cap.append((a, rem - 1, slots - 1))
            return a
    return None


# ── 提交 ──────────────────────────────────────────────────────────────────
def canon_op(op):
    'snake -> 平台规范算子名（CamelCase）。'
    import re as _re
    _norm = lambda s: _re.sub(r"[^a-z0-9]", "", s.lower())
    try:
        m = json.load(open(HERE / "op_canon.json"))
    except Exception:
        return op
    c = m.get(_norm(op))
    if c is None:
        print(f"  ⚠ {op}: 在 op_canon.json 里找不到规范名，按原样提交（很可能白交）")
        return op
    return c


def declared_ops(op):
    '决定 `selected_operators` 里放什么。'
    if os.environ.get("PUSH_DECLARE_ALL") != "1":
        return [canon_op(op)]
    try:
        m = json.load(open(HERE / "op_canon.json"))
    except Exception:
        print("  ⚠ op_canon.json 读不到，退回单算子声明")
        return [canon_op(op)]
    allops = sorted(set(m.values()))
    mine = canon_op(op)
    if mine not in allops:
        print(f"  ⚠ {op}: 规范名 {mine} 不在 op_canon.json 的值里，退回单算子声明")
        return [canon_op(op)]
    print(f"  声明全部 {len(allops)} 个算子（本发实际改的是 {mine}）")
    return allops


def stage_package(srcdir):
    """Copy template + operator tree. Returns (stage, pkg, op_dir). Caller rmtree stage."""
    tmpl = HERE.parent / "cann_submit" / "template"
    stage = tempfile.mkdtemp()
    pkg = os.path.join(stage, "pkg")
    shutil.copytree(tmpl, pkg)
    for d in pathlib.Path(pkg, "csrc", "ops").iterdir():
        if d.is_dir():
            shutil.rmtree(d)
    # 拷源码：跳过符号链接（candidates 里有自指的坏链 X -> X）
    # 910c_tasks/<op>/arm 的 basename 是 arm，必须落到 csrc/ops/<op>。
    op_dir = ops_dir_name(srcdir)
    shutil.copytree(srcdir, os.path.join(pkg, "csrc", "ops", op_dir),
                    symlinks=False,
                    ignore=shutil.ignore_patterns(
                        "._*", "__pycache__", "*.pyc", "*.pyo",
                        "verify_host.py"))
    return stage, pkg, op_dir


def submit(op, srcdir, tokvar):
    '打包一个算子的源码目录并提交（F17：一个 zip 一个算子）。'
    stage, pkg, op_dir = stage_package(srcdir)

    import gen_init
    exported = gen_init.sync(pkg)          # 解析不出 schema 会抛异常 —— 宁可不发
    want = op_dir
    if want not in exported:
        raise RuntimeError(
            f"{op}: 生成的 cann_bench 接口 {exported} 里没有 {want} —— 拒绝提交。"
            f"（runner 匹配的是这个名字，不是目录名/CamelCase 名）")
    print(f"  cann_bench 接口已同步: {exported}")

    to_lf_tree(pathlib.Path(pkg))
    zp = os.path.join(stage, "s.zip")
    write_zip(pathlib.Path(pkg), pathlib.Path(zp))
    http, data = W.submit_zip(zp, declared_ops(op), os.environ[tokvar], tokvar)
    shutil.rmtree(stage, ignore_errors=True)
    d = data if isinstance(data, dict) else {}
    return ((d.get("job") or {}).get("id"), (d.get("submission") or {}).get("id"), http)


def wait_job(jid, tokvar, timeout=2400):
    end = time.time() + timeout
    while time.time() < end:
        m = W.job_meta(jid, os.environ[tokvar])
        if m.get("status") in W.TERM:
            return m
        time.sleep(20)
    return {}


def fire_hidden(sub, tokvar):
    s, d = W.api(f"/api/submissions/{sub}/rerun-hidden", os.environ[tokvar],
                 "POST", b"{}", "application/json")
    if s == 200 and isinstance(d, dict):
        return (d.get("job") or {}).get("id"), None
    return None, f"http={s} {str(d)[:100]}"


def settle_standard(item):
    """Wait one standard and fire its hidden immediately when eligible.

    This function is intentionally side-effect-light apart from the hidden POST;
    trajectory writes stay in the main thread so concurrent completions cannot
    interleave JSONL records.
    """
    op, note, jid, tv = item
    m = wait_job(jid, tv)
    st, sub = m.get("status"), m.get("submission_id")
    out = {"op": op, "note": note, "jid": jid, "tv": tv,
           "status": st, "submission": sub, "hidden": None, "error": None}
    if sub and st == "succeeded":
        out["hidden"], out["error"] = fire_hidden(sub, tv)
    return out


def settle_hidden(item):
    op, note, hj, tv = item
    return item, wait_job(hj, tv)


def main():
    if "--log" in sys.argv:
        if TRAJ.exists():
            for ln in open(TRAJ):
                r = json.loads(ln)
                print(f"{r['t']}  {r.get('kind',''):10s} {r.get('op',''):26s} "
                      f"{r.get('note','')}  {r.get('detail','')}")
        return
    if "--sync" in sys.argv:
        tot, delta, moved = snapshot()
        print(f"总分 {tot:.2f}  Δ={delta:+.2f}")
        for op, cs, o, n, d in sorted(moved, key=lambda x: -abs(x[4])):
            print(f"  {op:26s} {cs:9s} {o:6.2f} -> {n:6.2f}  {d:+.2f}")
        rec(kind="SNAP", note=f"total={tot:.2f}", detail=f"delta={delta:+.2f} moved={len(moved)}")
        return

    check_gate_only = "--check-gate" in sys.argv
    require_gate = check_gate_only or os.environ.get("PUSH_REQUIRE_GATE") == "1"

    specs = []
    for a in sys.argv[1:]:
        if a == "--check-gate":
            continue
        specs.append(parse_spec(a))
    if not specs:
        print(__doc__); return

    if check_gate_only:
        failed = False
        for op, src, _note in specs:
            src_hash = source_fingerprint(src)
            gate_path, _gate, errors = validate_gate(
                op, src, src_hash, required=True)
            if errors:
                failed = True
                print(f"GATE FAIL {op}: " + "; ".join(errors))
            else:
                print(f"GATE PASS {op}: {gate_path} source_sha256={src_hash}")
        if failed:
            raise SystemExit(2)
        return

    # Validate every source before any leaderboard/account API call.  This
    # keeps a stale/missing required gate at zero remote cost.  The source hash
    # is checked again after the atomic submit claim to close the local
    # preflight-to-claim race.
    preflighted = []
    for op, src, note in specs:
        src_hash = source_fingerprint(src)
        gate_path, _gate, gate_errors = validate_gate(
            op, src, src_hash, required=require_gate)
        if gate_errors:
            detail = "; ".join(gate_errors)
            rec(kind="GATE_REJECT", op=op, note=note, src=src,
                source_sha256=src_hash, detail=detail)
            print(f"  ⊘ {op:26s} promotion gate 拒绝: {detail}")
            continue
        if gate_path is not None:
            print(f"  gate ✓ {op}: {gate_path}")
        preflighted.append((op, src, note, src_hash))
    if not preflighted:
        print("没有通过 promotion gate 的候选；未查询榜面/账号，未提交。")
        return

    tot0, _, _ = snapshot()
    print(f"起点 {tot0:.2f}   待推 {len(preflighted)} 项")
    cap = capacity()
    print("额度:", {a: r for a, r, s in cap if r > 0})

    live = []
    for op, src, note, gated_hash in preflighted:
        claim, src_hash, prior = acquire_submit_claim(op, src)
        if src_hash != gated_hash:
            release_submit_claim(claim)
            detail = (f"source changed after gate preflight: "
                      f"{gated_hash} -> {src_hash}")
            rec(kind="GATE_REJECT", op=op, note=note, src=src,
                source_sha256=src_hash, detail=detail)
            print(f"  ⊘ {op:26s} promotion gate 拒绝: {detail}")
            continue
        if prior is not None:
            old_job = prior.get("job") or "claim-in-flight"
            rec(kind="DEDUP_SKIP", op=op, note=note, src=src,
                detail=f"same source_sha256={src_hash} prior={old_job}")
            print(f"  ↷ {op:26s} 同源码已提交/在飞，跳过 ({old_job})")
            continue
        tv = pick(cap)
        if tv is None:
            cap = capacity(); tv = pick(cap)
        if tv is None:
            release_submit_claim(claim)
            rec(kind="SKIP", op=op, note=note, detail="全部账号无额度/无空槽")
            print(f"  ⏸ {op:26s} 无额度，跳过"); continue
        jid, sub, http = submit(op, src, tv)
        if not jid:
            release_submit_claim(claim)
            rec(kind="FAIL", op=op, note=note, acct=tv, src=src, http=http,
                detail=f"提交失败 http={http} acct={tv}")
            print(f"  ✗ {op:26s} 提交失败 http={http}"); continue
        finish_submit_claim(claim, job=jid, submission=sub, acct=tv, submitted_at=time.time())
        rec(kind="SUBMIT", op=op, note=note, job=jid, sub=sub, acct=tv, src=src,
            source_sha256=src_hash, http=http,
            detail=f"{jid} {sub} acct={tv} src={src}")
        print(f"  → {op:26s} {jid} {sub} ({tv})")
        live.append((op, note, jid, tv))

    # 并行等 standard：谁先落地谁立刻补 hidden。旧实现按参数顺序串行 wait，
    # 第一发慢时会把后面已经成功的 2–3 条链一起堵住。
    print("\n等 standard 落地并补 hidden …")
    fired = []
    if live:
        with cf.ThreadPoolExecutor(max_workers=len(live)) as ex:
            fs = [ex.submit(settle_standard, item) for item in live]
            for fut in cf.as_completed(fs):
                out = fut.result()
                op, note, jid, tv = out["op"], out["note"], out["jid"], out["tv"]
                st, sub = out["status"], out["submission"]
                rec(kind="LAND", op=op, note=note, detail=f"{jid} status={st}")
                if not sub:
                    print(f"  {op:26s} {st}  无 submission，不补 hidden")
                    continue
                if st != "succeeded":
                    rec(kind="HIDDEN_SKIP", op=op, note=note,
                        detail=f"standard status={st}，不补 hidden")
                    print(f"  {op:26s} {st:20s} standard 未成，跳过 hidden")
                    continue
                hj, err = out["hidden"], out["error"]
                if hj:
                    rec(kind="HIDDEN", op=op, note=note,
                        detail=f"{hj} acct={tv} from={sub}")
                    print(f"  {op:26s} {st:20s} hidden -> {hj}")
                    fired.append((op, note, hj, tv))
                else:
                    rec(kind="HIDDEN_FAIL", op=op, note=note, detail=str(err))
                    print(f"  {op:26s} {st:20s} hidden 失败 {err}")

    if fired:
        print("\n等 hidden 终态 …")
        with cf.ThreadPoolExecutor(max_workers=len(fired)) as ex:
            fs = [ex.submit(settle_hidden, item) for item in fired]
            for fut in cf.as_completed(fs):
                (op, note, hj, tv), m = fut.result()
                st = m.get("status")
                line = f"{hj} status={st}"
                for o in ((m.get("results") or {}).get("operators") or []):
                    line += (f" pass={o.get('passed_cases')}/{o.get('total_cases')}"
                             f" score={o.get('score')} sp={o.get('avg_speedup')}")
                rec(kind="LAND_HIDDEN", op=op, note=note, detail=line)
                mark = "✓" if st == "succeeded" else "🔴"
                print(f"  {mark} {op:24s} hidden {line}")

    time.sleep(30)
    tot, delta, moved = snapshot()
    print(f"\n榜面 {tot0:.2f} -> {tot:.2f}   Δ={delta:+.2f}")
    for o_, cs, a, b, d in sorted(moved, key=lambda x: -abs(x[4])):
        print(f"  {o_:26s} {cs:9s} {a:6.2f} -> {b:6.2f}  {d:+.2f}")
    rec(kind="ROUND", note=f"{tot0:.2f}->{tot:.2f}", detail=f"delta={delta:+.2f}")


if __name__ == "__main__":
    main()
