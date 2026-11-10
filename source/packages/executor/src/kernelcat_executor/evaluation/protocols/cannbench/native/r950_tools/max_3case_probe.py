#!/usr/bin/env python3
"""demo2 3-case Duration probe: Maximum parent vs mxv_schedbat_v1.

Uses the explicitly configured direct SSH connection.
C = sum of per-kernel Duration(us) medians from kernel_details.csv.
Uses run_evaluation.sh on card 10.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from kernelcat_executor.backends.ascend.transport.direct_ssh import ssh as school_ssh, ssh_argv

from kernel_c_score import CASE_PERF, s_of

PARENT = HERE / "livesrc910" / "maximum"
ARM = REPO / "910c_tasks" / "maximum" / "arm"
TEMPLATE = HERE.parent / "cann_submit" / "template"
OPERATOR = "Maximum"
TASK_REL = "level2/maximum"
OP_DIR = "maximum"
WANT = (5, 12, 19)
SKIP = ("CannBenchCacheClean", "cann_bench", "Profiler", "Step")


def cases_json() -> list[dict]:
    rows = []
    csv_path = HERE / "cases" / "maximum" / "cases.csv"
    want = set(WANT)
    with csv_path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            cid = int(row["case_id"])
            if cid not in want:
                continue
            rows.append({
                "operator": row["operator"],
                "case_id": cid,
                "input_shape": json.loads(row["input_shape"]),
                "dtype": json.loads(row["dtype"]),
                "attrs": json.loads(row["attrs"]),
                "value_range": json.loads(row["value_range"]),
            })
    rows.sort(key=lambda r: r["case_id"])
    if [r["case_id"] for r in rows] != list(WANT):
        raise SystemExit(f"expected cases {list(WANT)} got {[r['case_id'] for r in rows]}")
    return rows


def ssh_run(cmd: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return school_ssh(cmd, timeout=timeout)


def _blob(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data


def ssh_put(local: Path, remote: str, timeout: int = 180) -> subprocess.CompletedProcess:
    """Windows OpenSSH scp + ProxyCommand returns rc=1 with empty stderr. Pipe via ssh."""
    argv = ssh_argv(f"cat > {remote} && echo PUT_OK")
    with local.open("rb") as fh:
        return subprocess.run(argv, stdin=fh, capture_output=True, timeout=timeout)


def ssh_get_file(remote: str, local: Path, timeout: int = 180) -> None:
    argv = ssh_argv(f"cat {remote} 2>/dev/null || true")
    proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(proc.stdout or b"")


def csv_tar_cmd(remote: str) -> str:
    """Only kernel_details.csv. Never the profiler tree (Windows MAX_PATH)."""
    return (
        f"if [ -d {remote} ]; then "
        f"cd {remote} && find . -type f -name kernel_details.csv | tar czf - -T -; "
        f"fi"
    )


def ssh_get_dir(remote: str, local: Path, timeout: int = 180) -> None:
    import io
    argv = ssh_argv(csv_tar_cmd(remote))
    proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    blob = proc.stdout or b""
    if len(blob) < 40:
        print(f"csv tar empty rc={proc.returncode} err={_blob(proc.stderr)[-400:]!r}")
        return
    local.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name.replace("\\", "/").lstrip("./")
            if not name.endswith("kernel_details.csv") or "prof_data" in name:
                continue
            member.name = name
            try:
                tar.extract(member, local)
            except OSError as exc:
                print(f"skip {name}: {exc}")


def _tmp(prefix: str, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(fd)
    return Path(name)


_TEXT_SUFFIX = {
    ".sh", ".py", ".cmake", ".txt", ".md", ".json", ".yaml", ".yml",
    ".cfg", ".in", ".hpp", ".h", ".cpp", ".c", ".cc", ".cu",
}
_TEXT_NAMES = {"CMakeLists.txt", "Makefile", "Dockerfile", "build.sh"}


def _to_lf_tree(root: Path) -> None:
    """Windows checkout is CRLF; demo2 bash rejects $'\\r' in build.sh."""
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in _TEXT_SUFFIX and p.name not in _TEXT_NAMES:
            continue
        data = p.read_bytes()
        if b"\r" not in data:
            continue
        p.write_bytes(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))


def pack_source(src: Path, dest_tgz: Path) -> None:
    stage = Path(tempfile.mkdtemp(prefix="max3_pack_"))
    try:
        shutil.copytree(TEMPLATE, stage / "source")
        ops = stage / "source" / "csrc" / "ops"
        for p in ops.iterdir():
            if p.is_dir():
                shutil.rmtree(p)
        shutil.copytree(src, ops / OP_DIR)
        _to_lf_tree(stage / "source")
        with tarfile.open(dest_tgz, "w:gz") as tar:
            tar.add(stage / "source", arcname="source")
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def kernel_c_case(rep: Path, cid: int) -> float | None:
    files = list(rep.glob(f"**/maximum/{cid}/**/kernel_details.csv"))
    if not files:
        files = list(rep.glob(f"**/{cid}/**/kernel_details.csv"))
    by: dict[str, list[float]] = {}
    for path in files:
        with path.open(errors="ignore") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("Name") or row.get("name") or "").strip()
                dur = row.get("Duration(us)") or row.get("Duration(us )") or row.get("duration")
                if not name or dur in (None, ""):
                    continue
                if any(s.lower() in name.lower() for s in SKIP):
                    continue
                try:
                    by.setdefault(name, []).append(float(dur))
                except ValueError:
                    continue
    if not by:
        return None
    return sum(statistics.median(xs) for xs in by.values())


def score_rows(parent_c: dict[int, float], arm_c: dict[int, float]) -> dict:
    rows = []
    delta = 0.0
    for cid in WANT:
        b = parent_c.get(cid)
        c = arm_c.get(cid)
        rec = {"id": cid, "C_parent": b, "C_arm": c}
        if b is None or c is None or b <= 0:
            rec["ds"] = None
            rec["pts"] = None
        else:
            ds = s_of(c, b) - s_of(b, b)
            rec["ds"] = ds
            rec["pts"] = CASE_PERF * ds
            delta += rec["pts"]
        rows.append(rec)
    return {"rows": rows, "probe_pts": delta}


def write_remote_script(perf: str, cases_b64: str) -> str:
    # Keep this as a bash script with one python helper inlined.
    return "\n".join([
        "#!/bin/bash",
        "set -u",
        "REPO=/home/demo2/cann-bench-20260722",
        "OP=Maximum",
        "DEV=10",
        "WARMUP=3",
        "REPEAT=8",
        'RUN="$1"',
        'ARCHIVE="$2"',
        f"PERF={perf}",
        f"CASES_B64='{cases_b64}'",
        'rm -rf "$RUN"; mkdir -p "$RUN"',
        'tar xzf "$ARCHIVE" -C "$RUN" || exit 5',
        'TD="$RUN/task/level2/maximum"',
        'mkdir -p "$(dirname "$TD")"',
        'if [ -d "$REPO/tasks/level2/maximum" ]; then',
        '  cp -R "$REPO/tasks/level2/maximum" "$TD"',
        "else",
        '  found=$(find "$REPO/tasks" -maxdepth 3 -type d -name maximum | head -1)',
        '  [ -n "$found" ] || { echo NO_TASK; exit 6; }',
        '  mkdir -p "$TD"',
        '  cp -R "$found"/. "$TD/"',
        "fi",
        'chmod -R u+w "$TD"',
        "export TD CASES_B64",
        'python3 -c "import json,os,yaml,pathlib,base64; '
        "cases=json.loads(base64.b64decode(os.environ['CASES_B64'])); "
        "p=pathlib.Path(os.environ['TD'])/'cases.yaml'; "
        "d=yaml.safe_load(p.read_text()) if p.is_file() else {'cases':[]}; "
        "d['cases']=cases; "
        "p.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True)); "
        "print('kept',[int(c['case_id']) for c in d['cases']])"
        '"',
        'VENV=/tmp/venv_${DEV}',
        'if [ ! -x "$VENV/bin/python" ]; then python3 -m venv --system-site-packages "$VENV"; fi',
        '"$VENV/bin/pip" uninstall -y cann-bench cann_bench >/dev/null 2>&1 || true',
        '"$VENV/bin/python" -c "import build" 2>/dev/null || "$VENV/bin/pip" install -q build',
        "C9=/usr/local/Ascend/cann-9.0.0",
        'C9S="$RUN/cann9"; mkdir -p "$C9S/runtime"',
        'for e in "$C9"/*; do b=$(basename "$e"); [ "$b" = runtime ] || ln -s "$e" "$C9S/$b"; done',
        'for e in "$C9/runtime"/*; do b=$(basename "$e"); [ "$b" = version.info ] || ln -s "$e" "$C9S/runtime/$b"; done',
        'cp /usr/local/Ascend/ascend-toolkit/latest/runtime/version.info "$C9S/runtime/version.info" 2>/dev/null || true',
        'ARGS=(./scripts/run_evaluation.sh --source-dir "$RUN/source" --task-dir "$RUN/task" '
        '--operator "$OP" --device npu --device-id "$DEV" --reports-dir "$RUN/reports" '
        '--warmup "$WARMUP" --repeat "$REPEAT")',
        '[ "$PERF" = 1 ] || ARGS+=(--no-perf)',
        'echo "MARK eval start $(date +%T) perf=$PERF venv=$VENV"',
        "(",
        '  export PATH="$VENV/bin:$PATH" PIP_FORCE_REINSTALL=1 CANN_BUILD_JOBS=12',
        '  export ASCEND_HOME_PATH="$C9S"',
        '  export LD_LIBRARY_PATH="${C9}/lib64:${LD_LIBRARY_PATH:-}"',
        "  export TORCH_DEVICE_BACKEND_AUTOLOAD=0",
        '  cd "$REPO"; "${ARGS[@]}"',
        ') >"$RUN/evaluation.log" 2>&1',
        "rc=$?",
        'echo "MARK eval done rc=$rc $(date +%T)"',
        'tail -40 "$RUN/evaluation.log"',
        'echo "RUN_DIR=$RUN EVAL_RC=$rc"',
        "exit 0",
        "",
    ])


def run_arm(label: str, src: Path, fetch_dir: Path, perf: str = "1") -> dict:
    tag = f"max3_{label}_{int(time.time())}"
    remote_run = f"/tmp/{tag}"
    remote_tgz = f"/tmp/{tag}.tgz"
    remote_sh = f"/tmp/{tag}.sh"
    tgz = _tmp(tag + "_", ".tgz")
    pack_source(src, tgz)
    print(f"upload {label} {tgz} -> {remote_tgz} bytes={tgz.stat().st_size}")
    up = ssh_put(tgz, remote_tgz, timeout=180)
    tgz.unlink(missing_ok=True)
    up_out, up_err = _blob(up.stdout), _blob(up.stderr)
    print(f"put tgz rc={up.returncode} out={up_out[-200:]!r} err={up_err[-400:]!r}")
    if "PUT_OK" not in up_out:
        raise RuntimeError(f"put tgz failed rc={up.returncode} err={up_err[-800:]}")
    cases_b64 = __import__("base64").b64encode(json.dumps(cases_json()).encode()).decode("ascii")
    body = write_remote_script(perf, cases_b64)
    # Fix TD env: inject TD after copy
    body = body.replace(
        'python3 - <<\'PY\'',
        'export TD="$TD"\npython3 - <<\'PY\'',
    )
    sh = _tmp(tag + "_", ".sh")
    sh.write_text(body, encoding="utf-8", newline="\n")
    up2 = ssh_put(sh, remote_sh, timeout=60)
    sh.unlink(missing_ok=True)
    up2_out, up2_err = _blob(up2.stdout), _blob(up2.stderr)
    print(f"put sh rc={up2.returncode} out={up2_out[-200:]!r} err={up2_err[-400:]!r}")
    if "PUT_OK" not in up2_out:
        raise RuntimeError(f"put sh failed rc={up2.returncode} err={up2_err[-800:]}")
    remote_out = f"{remote_run}.out"
    launch = ssh_run(
        f"nohup bash {remote_sh} {remote_run} {remote_tgz} > {remote_out} 2>&1 < /dev/null & "
        f"echo LAUNCH_PID=$!",
        timeout=40,
    )
    print(launch.stdout or "")
    if launch.stderr:
        print("launch stderr", launch.stderr[-400:])
    if "LAUNCH_PID=" not in (launch.stdout or ""):
        raise RuntimeError(f"launch failed rc={launch.returncode} err={launch.stderr}")
    deadline = time.time() + 3600
    done = False
    last = ""
    while time.time() < deadline:
        time.sleep(20)
        chk = ssh_run(
            f"if grep -q 'MARK eval done' {remote_out} 2>/dev/null; then echo EVAL_DONE; tail -20 {remote_out}; "
            f"elif grep -q 'MARK eval start' {remote_out} 2>/dev/null; then echo EVAL_RUN; tail -2 {remote_out}; "
            f"else echo EVAL_WAIT; wc -c {remote_out} 2>/dev/null || true; fi",
            timeout=40,
        )
        last = chk.stdout or ""
        print(last[-600:])
        if "EVAL_DONE" in last:
            done = True
            break
    if not done:
        print("eval poll timeout", remote_run, last[-400:])
    return collect_run(label, remote_run, fetch_dir, done=done, remote_out=remote_out)


def collect_run(
    label: str,
    remote_run: str,
    fetch_dir: Path,
    *,
    done: bool,
    remote_out: str | None = None,
) -> dict:
    fetch_dir.mkdir(parents=True, exist_ok=True)
    ssh_get_dir(f"{remote_run}/reports", fetch_dir / "reports", timeout=180)
    ssh_get_file(f"{remote_run}/evaluation.log", fetch_dir / "evaluation.log", timeout=60)
    if remote_out:
        ssh_get_file(remote_out, fetch_dir / "remote.out", timeout=60)
    log = fetch_dir / "evaluation.log"
    tail = log.read_text(errors="ignore")[-8000:] if log.is_file() else ""
    cs = {}
    reports = fetch_dir / "reports"
    if reports.is_dir():
        for cid in WANT:
            cs[cid] = kernel_c_case(reports, cid)
        if any(cs.get(cid) is None for cid in WANT):
            for jp in sorted(reports.glob("cann_performance_eval*.json")):
                try:
                    data = json.loads(jp.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                for op in data.get("operators") or []:
                    for case in op.get("cases") or []:
                        cid_s = str(case.get("case_id") or "")
                        try:
                            cid = int(cid_s.rsplit("_", 1)[-1])
                        except ValueError:
                            continue
                        if cid not in WANT or cs.get(cid) is not None:
                            continue
                        kernels = ((case.get("op_times") or {}).get("device_kernels") or {})
                        if kernels:
                            cs[cid] = sum(float(v) for v in kernels.values())
    print(f"{label} C={cs} done={done}")
    return {
        "label": label,
        "remote": remote_run,
        "ssh_rc": 0 if done else 124,
        "C": cs,
        "log_tail": tail[-4000:],
    }


def main() -> int:
    if not PARENT.is_dir() or not ARM.is_dir():
        print("missing parent or arm", PARENT, ARM)
        return 2
    if "--pack-only" in sys.argv:
        dest = _tmp("max3_pack_", ".tgz")
        pack_source(ARM, dest)
        n = dest.stat().st_size
        crlf = False
        with tarfile.open(dest, "r:gz") as tar:
            fh = tar.extractfile("source/build.sh")
            blob = fh.read() if fh else b""
            crlf = b"\r" in blob
        print(f"PACK_OK bytes={n} path={dest} {'PACK_CRLF' if crlf else 'PACK_LF'}")
        dest.unlink(missing_ok=True)
        print("cases", [c["case_id"] for c in cases_json()])
        return 0 if n > 1000 and not crlf else 1
    ping = ssh_run("echo SCHOOL_OK; whoami; hostname", timeout=40)
    print(ping.stdout)
    if "SCHOOL_OK" not in (ping.stdout or ""):
        print("ssh failed", ping.returncode, ping.stderr)
        return 3
    leftover = ssh_run(
        "ps -ef | grep -E 'run_evaluation.sh|max3_parent_|max3_arm_' | grep -v grep || true",
        timeout=40,
    )
    print("leftover", leftover.stdout or "")
    out = Path(os.environ.get("MAX3_PROBE_OUT") or str(HERE / ".max3_probe"))
    out.mkdir(parents=True, exist_ok=True)
    parent_remote = os.environ.get("MAX3_PARENT_REMOTE", "").strip()
    if parent_remote:
        print("reuse parent remote", parent_remote)
        parent = collect_run("parent", parent_remote, out / "parent", done=True)
    else:
        parent = run_arm("parent", PARENT, out / "parent", perf="1")
    arm = run_arm("arm", ARM, out / "arm", perf="1")
    scored = score_rows(parent["C"], arm["C"])
    rec = {"parent": parent, "arm": arm, "score": scored}
    (out / "summary.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(scored, indent=2))
    print("probe_pts", scored["probe_pts"])
    missing = [cid for cid in WANT if parent["C"].get(cid) is None or arm["C"].get(cid) is None]
    if missing:
        print("PROBE_INCOMPLETE missing Duration C for", missing)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
