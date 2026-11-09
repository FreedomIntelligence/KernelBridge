#!/usr/bin/env python3
"""demo2 20-case Duration A/B vs livesrc parent via run_evaluation.sh.

C = per-case sum of device-kernel Duration(us) from the performance JSON
(device_kernels), falling back to kernel_details.csv medians.
Does not invoke ab910.sh.

  python ab20_duration.py --op scatter --pack-only
  python ab20_duration.py --op scatter --parent-dev 10 --arm-dev 11
"""
from __future__ import annotations

import argparse
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

from school_sdk.ssh import ssh as school_ssh, ssh_exec
from kernel_c_score import CASE_PERF, s_of

TEMPLATE = HERE.parent / "cann_submit" / "template"
SKIP = ("CannBenchCacheClean", "cann_bench", "Profiler", "Step")
WANT = tuple(range(1, 21))
OPS = {
    "scatter": {"camel": "Scatter", "level": "level2"},
    "nms": {"camel": "NMS", "level": "level3"},
    "gcd": {"camel": "Gcd", "level": "level2"},
    "cummin": {"camel": "Cummin", "level": "level2"},
    "roi_align": {"camel": "ROIAlign", "level": "level3"},
    "conv_3d_backprop_filter": {"camel": "Conv3DBackpropFilter", "level": "level3"},
    "mha": {"camel": "MHA", "level": "level4"},
    "gqa": {"camel": "GQA", "level": "level4"},
    "grid_sampler_3d": {"camel": "GridSampler3D", "level": "level2"},
    "group_norm": {"camel": "GroupNorm", "level": "level2"},
    "moe_finalize_routing": {"camel": "MoeFinalizeRouting", "level": "level3"},
    "mla": {"camel": "MLA", "level": "level4"},
    "cross_entropy_loss": {"camel": "CrossEntropyLoss", "level": "level2"},
    "unsorted_segment_sum": {"camel": "UnsortedSegmentSum", "level": "level2"},
    "gather": {"camel": "Gather", "level": "level2"},
    "transpose": {"camel": "Transpose", "level": "level3"},
    "sparse_flash_attention": {"camel": "SparseFlashAttention", "level": "level4"},
    "conv_2d": {"camel": "Conv2D", "level": "level3"},
    "engram_gate_fusion": {"camel": "EngramGateFusion", "level": "level3"},
    "dilation_2d": {"camel": "Dilation2D", "level": "level3"},
    "dynamic_quant": {"camel": "DynamicQuant", "level": "level2"},
    "weight_quant_batch_matmul": {"camel": "WeightQuantBatchMatmul", "level": "level3"},
    "top_k": {"camel": "TopK", "level": "level3"},
    "grouped_matmul": {"camel": "GroupedMatmul", "level": "level3"},
}


def spec_for(snake: str) -> dict:
    if snake not in OPS:
        raise SystemExit(f"unknown op {snake}; choose {sorted(OPS)}")
    meta = dict(OPS[snake])
    meta["snake"] = snake
    parent_override = os.environ.get("AB20_PARENT_SOURCE")
    arm_override = os.environ.get("AB20_ARM_SOURCE")
    meta["parent"] = (
        Path(parent_override).resolve()
        if parent_override
        else HERE / "livesrc910" / snake
    )
    meta["arm"] = (
        Path(arm_override).resolve()
        if arm_override
        else REPO / "910c_tasks" / snake / "arm"
    )
    meta["csv"] = HERE / "cases" / snake / "cases.csv"
    return meta


def cases_json(snake: str, ids: tuple[int, ...] | None = None) -> list[dict]:
    want = ids or WANT
    csv_path = spec_for(snake)["csv"]
    rows = []
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
    got = [r["case_id"] for r in rows]
    if got != sorted(want):
        raise SystemExit(f"{snake}: expected cases {sorted(want)} got {got}")
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
    data = local.read_bytes()
    last: subprocess.CompletedProcess | BaseException | None = None
    for attempt in range(3):
        try:
            proc = ssh_exec(f"cat > {remote} && echo PUT_OK", timeout=timeout, stdin=data)
            if "PUT_OK" in _blob(proc.stdout):
                return proc
            last = proc
            print(f"ssh_put attempt {attempt + 1} missing PUT_OK rc={proc.returncode}")
        except (OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
            last = exc
            print(f"ssh_put attempt {attempt + 1} failed: {type(exc).__name__}: {exc}")
        except Exception as exc:
            last = exc
            print(f"ssh_put attempt {attempt + 1} failed: {type(exc).__name__}: {exc}")
        time.sleep(5)
    if isinstance(last, BaseException):
        raise last
    if last is None:
        raise RuntimeError("ssh_put exhausted retries")
    return last


def ssh_get_file(remote: str, local: Path, timeout: int = 180) -> None:
    proc = ssh_exec(f"cat {remote} 2>/dev/null || true", timeout=timeout)
    local.parent.mkdir(parents=True, exist_ok=True)
    blob = proc.stdout or b""
    local.write_bytes(blob if isinstance(blob, bytes) else blob.encode("utf-8", "replace"))


def report_tar_cmd(remote: str) -> str:
    return (
        f"if [ -d {remote} ]; then "
        f"cd {remote} && find . -type f \\( -name kernel_details.csv "
        f"-o -name 'cann_performance_eval*.json' \\) | tar czf - -T -; "
        f"fi"
    )


def ssh_get_dir(remote: str, local: Path, timeout: int = 180) -> None:
    import io
    proc = ssh_exec(report_tar_cmd(remote), timeout=timeout)
    blob = proc.stdout or b""
    if isinstance(blob, str):
        blob = blob.encode("latin1")
    if len(blob) < 40:
        print(f"report tar empty rc={proc.returncode} err={_blob(proc.stderr)[-400:]!r}")
        return
    local.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name.replace("\\", "/").lstrip("./")
            if "prof_data" in name and not name.endswith("kernel_details.csv"):
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
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in _TEXT_SUFFIX and p.name not in _TEXT_NAMES:
            continue
        data = p.read_bytes()
        if b"\r" not in data:
            continue
        p.write_bytes(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))


def pack_source(src: Path, dest_tgz: Path, snake: str) -> None:
    stage = Path(tempfile.mkdtemp(prefix="ab20_pack_"))
    try:
        shutil.copytree(TEMPLATE, stage / "source")
        ops = stage / "source" / "csrc" / "ops"
        for p in list(ops.iterdir()):
            if p.is_dir():
                shutil.rmtree(p)
        shutil.copytree(
            src,
            ops / snake,
            ignore=shutil.ignore_patterns(
                "._*", "__pycache__", "*.pyc", "verify_host.py", "*.md"
            ),
        )
        _to_lf_tree(stage / "source")
        with tarfile.open(dest_tgz, "w:gz") as tar:
            tar.add(stage / "source", arcname="source")
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def kernel_c_case(rep: Path, snake: str, cid: int) -> float | None:
    files = list(rep.glob(f"**/{snake}/{cid}/**/kernel_details.csv"))
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


def c_from_perf_json(path: Path) -> dict[int, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[int, float] = {}
    for op in data.get("operators") or []:
        for case in op.get("cases") or []:
            cid_s = str(case.get("case_id") or "")
            try:
                cid = int(cid_s.rsplit("_", 1)[-1])
            except ValueError:
                continue
            kernels = ((case.get("op_times") or {}).get("device_kernels") or {})
            if not kernels:
                continue
            out[cid] = sum(float(v) for v in kernels.values())
    return out


def score_20(
    parent_c: dict[int, float | None],
    arm_c: dict[int, float | None],
    ids: tuple[int, ...] | None = None,
) -> dict:
    rows = []
    pts = 0.0
    wins = 0
    missing = []
    parent_sum = 0.0
    arm_sum = 0.0
    for cid in (ids or WANT):
        b = parent_c.get(cid)
        c = arm_c.get(cid)
        rec = {"id": cid, "C_parent": b, "C_arm": c}
        if not isinstance(b, (int, float)) or not isinstance(c, (int, float)) or b <= 0:
            rec["ds"] = None
            rec["pts"] = None
            missing.append(cid)
        else:
            ds = s_of(c, b) - s_of(b, b)
            rec["ds"] = ds
            rec["pts"] = CASE_PERF * ds
            pts += rec["pts"]
            parent_sum += b
            arm_sum += c
            if c < b:
                wins += 1
        rows.append(rec)
    return {
        "rows": rows,
        "probe_pts": pts,
        "wins": wins,
        "missing": missing,
        "C_parent_sum": parent_sum,
        "C_arm_sum": arm_sum,
        "arm_beats_parent": bool(
            arm_sum and parent_sum and arm_sum < parent_sum and not missing
        ),
    }


def write_remote_script(spec: dict, dev: int, perf: str, cases_b64: str) -> str:
    snake, camel, level = spec["snake"], spec["camel"], spec["level"]
    return "\n".join([
        "#!/bin/bash",
        "set -u",
        "REPO=/home/demo2/cann-bench-20260722",
        f"OP={camel}",
        f"DEV={dev}",
        "WARMUP=3",
        "REPEAT=5",
        'RUN="$1"',
        'ARCHIVE="$2"',
        f"PERF={perf}",
        f"CASES_B64='{cases_b64}'",
        'rm -rf "$RUN"; mkdir -p "$RUN"',
        'tar xzf "$ARCHIVE" -C "$RUN" || exit 5',
        f'TD="$RUN/task/{level}/{snake}"',
        'mkdir -p "$(dirname "$TD")"',
        f'if [ -d "$REPO/tasks/{level}/{snake}" ]; then',
        f'  cp -R "$REPO/tasks/{level}/{snake}" "$TD"',
        "else",
        f'  found=$(find "$REPO/tasks" -maxdepth 3 -type d -name {snake} | head -1)',
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


def launch_one(
    label: str,
    spec: dict,
    src: Path,
    dev: int,
    perf: str = "1",
    ids: tuple[int, ...] | None = None,
) -> tuple[str, str]:
    snake = spec["snake"]
    tag = f"ab20_{snake}_{label}_{int(time.time())}"
    remote_run = f"/tmp/{tag}"
    remote_tgz = f"/tmp/{tag}.tgz"
    remote_sh = f"/tmp/{tag}.sh"
    tgz = _tmp(tag + "_", ".tgz")
    pack_source(src, tgz, snake)
    print(f"upload {snake}/{label} bytes={tgz.stat().st_size} -> {remote_tgz}")
    up = ssh_put(tgz, remote_tgz, timeout=180)
    tgz.unlink(missing_ok=True)
    up_out = _blob(up.stdout)
    print(f"put tgz rc={up.returncode} out={up_out[-80:]!r}")
    if "PUT_OK" not in up_out:
        raise RuntimeError(f"put tgz failed err={_blob(up.stderr)[-800:]}")
    cases_b64 = __import__("base64").b64encode(
        json.dumps(cases_json(snake, ids)).encode()
    ).decode("ascii")
    body = write_remote_script(spec, dev, perf, cases_b64)
    sh = _tmp(tag + "_", ".sh")
    sh.write_text(body, encoding="utf-8", newline="\n")
    up2 = ssh_put(sh, remote_sh, timeout=60)
    sh.unlink(missing_ok=True)
    up2_out = _blob(up2.stdout)
    print(f"put sh rc={up2.returncode} out={up2_out[-80:]!r}")
    if "PUT_OK" not in up2_out:
        raise RuntimeError(f"put sh failed err={_blob(up2.stderr)[-800:]}")
    remote_out = f"{remote_run}.out"
    launch = ssh_run(
        f"nohup bash {remote_sh} {remote_run} {remote_tgz} > {remote_out} 2>&1 < /dev/null & "
        f"echo LAUNCH_PID=$!",
        timeout=40,
    )
    print(launch.stdout or "")
    if "LAUNCH_PID=" not in (launch.stdout or ""):
        raise RuntimeError(f"launch failed rc={launch.returncode} err={launch.stderr}")
    return remote_run, remote_out


def poll_one(remote_out: str, timeout_s: int = 5400) -> bool:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        time.sleep(20)
        chk = ssh_run(
            f"if grep -q 'MARK eval done' {remote_out} 2>/dev/null; then echo EVAL_DONE; tail -24 {remote_out}; "
            f"elif grep -q 'MARK eval start' {remote_out} 2>/dev/null; then echo EVAL_RUN; tail -2 {remote_out}; "
            f"else echo EVAL_WAIT; wc -c {remote_out} 2>/dev/null || true; fi",
            timeout=180,
        )
        last = chk.stdout or ""
        print(last[-700:])
        if "EVAL_DONE" in last:
            return True
    print("eval poll timeout", remote_out, last[-400:])
    return False


def collect_run(
    label: str,
    spec: dict,
    remote_run: str,
    fetch_dir: Path,
    *,
    done: bool,
    remote_out: str | None = None,
    ids: tuple[int, ...] | None = None,
) -> dict:
    snake = spec["snake"]
    fetch_dir.mkdir(parents=True, exist_ok=True)
    ssh_get_dir(f"{remote_run}/reports", fetch_dir / "reports", timeout=180)
    ssh_get_file(f"{remote_run}/evaluation.log", fetch_dir / "evaluation.log", timeout=60)
    if remote_out:
        ssh_get_file(remote_out, fetch_dir / "remote.out", timeout=60)
    log = fetch_dir / "evaluation.log"
    tail = log.read_text(errors="ignore")[-8000:] if log.is_file() else ""
    cs: dict[int, float | None] = {cid: None for cid in (ids or WANT)}
    reports = fetch_dir / "reports"
    if reports.is_dir():
        for jp in sorted(reports.glob("**/cann_performance_eval*.json")):
            parsed = c_from_perf_json(jp)
            for cid, val in parsed.items():
                if cid in cs and cs[cid] is None:
                    cs[cid] = val
        for cid in (ids or WANT):
            if cs[cid] is None:
                cs[cid] = kernel_c_case(reports, snake, cid)
    print(f"{snake}/{label} C={cs} done={done}")
    return {
        "label": label,
        "remote": remote_run,
        "ssh_rc": 0 if done else 124,
        "C": cs,
        "log_tail": tail[-4000:],
    }


def pack_only(snake: str) -> int:
    spec = spec_for(snake)
    dest = _tmp(f"ab20_{snake}_", ".tgz")
    pack_source(spec["arm"], dest, snake)
    n = dest.stat().st_size
    crlf = False
    names = []
    with tarfile.open(dest, "r:gz") as tar:
        names = tar.getnames()
        fh = tar.extractfile("source/build.sh")
        blob = fh.read() if fh else b""
        crlf = b"\r" in blob
    dest.unlink(missing_ok=True)
    print(f"PACK_OK op={snake} bytes={n} {'PACK_CRLF' if crlf else 'PACK_LF'}")
    print("cases", [c["case_id"] for c in cases_json(snake)])
    op_ok = any(x == f"source/csrc/ops/{snake}" or x.startswith(f"source/csrc/ops/{snake}/") for x in names)
    arm_bad = any("/arm/" in x or x.endswith("/arm") for x in names)
    print("PACK_NAME_OK" if op_ok and not arm_bad else "PACK_NAME_BAD")
    return 0 if n > 1000 and not crlf and op_ok and not arm_bad else 1


def run_ab(
    snake: str,
    parent_dev: int,
    arm_dev: int,
    ids: tuple[int, ...] | None = None,
    serial: bool = False,
) -> int:
    spec = spec_for(snake)
    parent, arm = spec["parent"], spec["arm"]
    if not parent.is_dir() or not arm.is_dir():
        print("missing parent or arm", parent, arm)
        return 2
    ping = ssh_run("echo SCHOOL_OK; whoami; hostname", timeout=40)
    print(ping.stdout)
    if "SCHOOL_OK" not in (ping.stdout or ""):
        print("ssh failed", ping.returncode, ping.stderr)
        return 3
    leftover = ssh_run(
        f"ps -ef | grep -E 'run_evaluation.sh|ab20_{snake}_' | grep -v grep || true",
        timeout=180,
    )
    print("leftover", leftover.stdout or "")
    out = Path(os.environ.get("AB20_OUT") or str(HERE / f".ab20_{snake}"))
    out.mkdir(parents=True, exist_ok=True)
    parent_run, parent_out = launch_one("parent", spec, parent, parent_dev, ids=ids)
    if serial:
        parent_done = poll_one(parent_out)
        arm_run, arm_out = launch_one("arm", spec, arm, arm_dev, ids=ids)
        arm_done = poll_one(arm_out)
    else:
        arm_run, arm_out = launch_one("arm", spec, arm, arm_dev, ids=ids)
        parent_done = poll_one(parent_out)
        arm_done = poll_one(arm_out)
    parent_rec = collect_run(
        "parent", spec, parent_run, out / "parent",
        done=parent_done, remote_out=parent_out, ids=ids,
    )
    arm_rec = collect_run(
        "arm", spec, arm_run, out / "arm",
        done=arm_done, remote_out=arm_out, ids=ids,
    )
    scored = score_20(parent_rec["C"], arm_rec["C"], ids=ids)
    rec = {"parent": parent_rec, "arm": arm_rec, "score": scored, "op": snake}
    (out / "summary.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: scored[k] for k in ("probe_pts", "wins", "missing", "C_parent_sum", "C_arm_sum", "arm_beats_parent")}, indent=2))
    if scored["missing"]:
        print("AB_INCOMPLETE missing", scored["missing"])
        return 4
    if scored["arm_beats_parent"]:
        print(f"AB_WIN {snake} C {scored['C_parent_sum']:.4f} -> {scored['C_arm_sum']:.4f} pts={scored['probe_pts']:+.4f}")
        return 0
    print(f"AB_NOWIN {snake} C {scored['C_parent_sum']:.4f} -> {scored['C_arm_sum']:.4f} pts={scored['probe_pts']:+.4f}")
    return 5


def main() -> int:
    p = argparse.ArgumentParser(description="20-case Duration A/B via run_evaluation.sh")
    p.add_argument("--op", required=True, choices=sorted(OPS))
    p.add_argument("--pack-only", action="store_true")
    p.add_argument("--parent-dev", type=int, default=10)
    p.add_argument("--arm-dev", type=int, default=11)
    p.add_argument("--serial", action="store_true", help="parent then arm on the same timeline")
    p.add_argument("--ids", default="", help="comma case ids (default 1-20)")
    ns = p.parse_args()
    ids = None
    if ns.ids.strip():
        ids = tuple(int(x.strip()) for x in ns.ids.split(",") if x.strip())
        if not ids:
            raise SystemExit("empty --ids")
    if ns.pack_only:
        return pack_only(ns.op)
    return run_ab(ns.op, ns.parent_dev, ns.arm_dev, ids=ids, serial=ns.serial)


if __name__ == "__main__":
    raise SystemExit(main())
