#!/usr/bin/env python3
"""Maximum 20-case Duration A/B on demo2 cards 10,11 via school_sdk ssh.

Does NOT use abctl. Remote driver is share ab910.sh with AB_LOCAL=1.
C = kernel_details.csv Duration(us) median sum per case.
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

from school_sdk.ssh import ssh as school_ssh, ssh_argv
from kernel_c_score import CASE_PERF, s_of
from push910 import to_lf_tree

PARENT = HERE / "livesrc910" / "maximum"
ARM = REPO / "910c_tasks" / "maximum" / "arm"
OP_DIR = "maximum"
SKIP = ("CannBenchCacheClean", "cann_bench", "Profiler", "Step")
REMOTE_AB910 = "/models/share/ka/cann_campaign/r950_tools/ab910.sh"
REMOTE_BENCH = "/home/demo2/cann-bench-20260722"
DEVS = "10 11"


def ssh_run(cmd: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return school_ssh(cmd, timeout=timeout)


def _blob(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data


def ssh_put(local: Path, remote: str, timeout: int = 180) -> subprocess.CompletedProcess:
    argv = ssh_argv(f"cat > {remote} && echo PUT_OK")
    with local.open("rb") as fh:
        return subprocess.run(argv, stdin=fh, capture_output=True, timeout=timeout)


def ssh_get_file(remote: str, local: Path, timeout: int = 180) -> None:
    argv = ssh_argv(f"cat {remote} 2>/dev/null || true")
    proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(proc.stdout or b"")


def pack_op_tree(src: Path, dest_tgz: Path, op_dir: str = OP_DIR) -> None:
    stage = Path(tempfile.mkdtemp(prefix="max_pack_"))
    try:
        shutil.copytree(
            src,
            stage / op_dir,
            ignore=shutil.ignore_patterns(
                "._*", "__pycache__", "*.pyc", "verify_host.py", "*.md"
            ),
        )
        to_lf_tree(stage / op_dir)
        with tarfile.open(dest_tgz, "w:gz") as tar:
            tar.add(stage / op_dir, arcname=op_dir)
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


def score_20(parent_c: dict[int, float | None], arm_c: dict[int, float | None]) -> dict:
    rows = []
    pts = 0.0
    wins = 0
    missing = []
    for cid in range(1, 21):
        b = parent_c.get(cid)
        c = arm_c.get(cid)
        rec = {"id": cid, "C_parent": b, "C_arm": c}
        if b is None or c is None or b <= 0:
            rec["ds"] = None
            rec["pts"] = None
            missing.append(cid)
        else:
            ds = s_of(c, b) - s_of(b, b)
            rec["ds"] = ds
            rec["pts"] = CASE_PERF * ds
            pts += rec["pts"]
            if c < b:
                wins += 1
        rows.append(rec)
    parent_sum = sum(v for v in parent_c.values() if isinstance(v, (int, float)))
    arm_sum = sum(v for v in arm_c.values() if isinstance(v, (int, float)))
    return {
        "rows": rows,
        "probe_pts": pts,
        "wins": wins,
        "missing": missing,
        "C_parent_sum": parent_sum,
        "C_arm_sum": arm_sum,
        "arm_beats_parent": bool(arm_sum and parent_sum and arm_sum < parent_sum and not missing),
    }


def remote_launch_cmd(tag: str) -> str:
    return (
        f"export AB_LOCAL=1 AB_DEVS='{DEVS}' AB_CONC=2 AB_TAG={tag} AB_CANN=9 "
        f"CANN_BENCH_HOME={REMOTE_BENCH} AB_OUT=/tmp/{tag}/out "
        f"CANN_BUILD_JOBS=12 TORCH_DEVICE_BACKEND_AUTOLOAD=0; "
        f"nohup bash {REMOTE_AB910} Maximum 2 "
        f"base:/tmp/{tag}/base/{OP_DIR} mt:/tmp/{tag}/mt/{OP_DIR} "
        f"> /tmp/{tag}/ab.log 2>&1 < /dev/null & echo LAUNCH_PID=$!"
    )


def main() -> int:
    if "--pack-only" in sys.argv:
        fd, name = tempfile.mkstemp(prefix="max_arm_", suffix=".tgz")
        os.close(fd)
        dest = Path(name)
        pack_op_tree(ARM, dest)
        with tarfile.open(dest, "r:gz") as tar:
            names = tar.getnames()
        dest.unlink(missing_ok=True)
        print("PACK_OK", names[:5])
        if any(n == OP_DIR or n.startswith(OP_DIR + "/") for n in names) and "arm/" not in "".join(names):
            print("PACK_NAME_OK")
            return 0
        print("PACK_NAME_BAD", names)
        return 1

    if not PARENT.is_dir() or not ARM.is_dir():
        print("missing parent or arm", PARENT, ARM)
        return 2

    ping = ssh_run("echo SCHOOL_OK; whoami", timeout=40)
    print(ping.stdout)
    if "SCHOOL_OK" not in (ping.stdout or ""):
        print("ssh failed", ping.returncode, ping.stderr)
        return 3

    tag = f"maxsched_{int(time.time())}"
    remote = f"/tmp/{tag}"
    ssh_run(f"mkdir -p {remote}/base {remote}/mt {remote}/out && echo MKDIR_OK", timeout=40)

    for label, src in (("base", PARENT), ("mt", ARM)):
        fd, name = tempfile.mkstemp(prefix=f"max_{label}_", suffix=".tgz")
        os.close(fd)
        tgz = Path(name)
        pack_op_tree(src, tgz)
        print(f"upload {label} bytes={tgz.stat().st_size}")
        up = ssh_put(tgz, f"{remote}/{label}.tgz", timeout=180)
        tgz.unlink(missing_ok=True)
        out = _blob(up.stdout)
        print(f"put {label} rc={up.returncode} out={out[-80:]!r}")
        if "PUT_OK" not in out:
            print("upload failed", _blob(up.stderr)[-400:])
            return 4
        x = ssh_run(
            f"tar xzf {remote}/{label}.tgz -C {remote}/{label} && "
            f"test -d {remote}/{label}/{OP_DIR}/op_kernel && echo EXTRACT_OK",
            timeout=40,
        )
        print(x.stdout)
        if "EXTRACT_OK" not in (x.stdout or ""):
            print("extract failed", x.stderr)
            return 5

    launch = ssh_run(remote_launch_cmd(tag), timeout=40)
    print(launch.stdout or "")
    if "LAUNCH_PID=" not in (launch.stdout or ""):
        print("launch failed", launch.stderr)
        return 6

    deadline = time.time() + 7200
    done = False
    last = ""
    while time.time() < deadline:
        time.sleep(30)
        chk = ssh_run(
            f"if grep -q 'AB_OUT=' /tmp/{tag}/ab.log 2>/dev/null; then echo AB_DONE; tail -30 /tmp/{tag}/ab.log; "
            f"elif grep -q '计时' /tmp/{tag}/ab.log 2>/dev/null; then echo AB_RUN; tail -8 /tmp/{tag}/ab.log; "
            f"else echo AB_WAIT; wc -c /tmp/{tag}/ab.log 2>/dev/null || true; fi",
            timeout=40,
        )
        last = chk.stdout or ""
        print(last[-800:])
        if "AB_DONE" in last:
            done = True
            break
    if not done:
        print("AB poll timeout", last[-400:])
        return 7

    out_dir = Path(os.environ.get("MAX_AB_OUT") or str(HERE / ".max_ab"))
    out_dir.mkdir(parents=True, exist_ok=True)
    ssh_get_file(f"/tmp/{tag}/ab.log", out_dir / "ab.log", timeout=60)
    import io
    argv = ssh_argv(
        f"if [ -d /tmp/ab_{tag} ]; then cd /tmp/ab_{tag} && "
        f"find . -type f -name kernel_details.csv | tar czf - -T -; fi"
    )
    proc = subprocess.run(argv, capture_output=True, timeout=180)
    blob = proc.stdout or b""
    csv_dir = out_dir / "csv"
    csv_dir.mkdir(exist_ok=True)
    if len(blob) >= 40:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for member in tar.getmembers():
                name = member.name.replace("\\", "/").lstrip("./")
                if not name.endswith("kernel_details.csv") or "prof_data" in name:
                    continue
                member.name = name
                try:
                    tar.extract(member, csv_dir)
                except OSError as exc:
                    print("skip", name, exc)

    parent_files = [p for p in csv_dir.rglob("kernel_details.csv") if "rep_base_" in str(p).replace("\\", "/")]
    arm_files = [p for p in csv_dir.rglob("kernel_details.csv") if "rep_mt_" in str(p).replace("\\", "/")]

    def c_from(files: list[Path]) -> dict[int, float | None]:
        if not files:
            return {i: None for i in range(1, 21)}
        tmp = Path(tempfile.mkdtemp(prefix="max_c_"))
        try:
            for f in files:
                rel = f.relative_to(csv_dir)
                dest = tmp / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
            return {cid: kernel_c_case(tmp, cid) for cid in range(1, 21)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if parent_files:
        parent_c = c_from(parent_files)
    if arm_files:
        arm_c = c_from(arm_files)
    if not parent_files or not arm_files:
        print("csv split incomplete", "base", len(parent_files), "mt", len(arm_files))

    scored = score_20(parent_c, arm_c)
    (out_dir / "summary.json").write_text(
        json.dumps({"tag": tag, "score": scored, "done": done}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({k: scored[k] for k in ("probe_pts", "wins", "missing", "C_parent_sum", "C_arm_sum", "arm_beats_parent")}, indent=2))
    if scored["arm_beats_parent"]:
        print("ARM_BEATS_PARENT")
        return 0
    print("ARM_NOT_FASTER")
    return 0 if not scored["missing"] else 8


if __name__ == "__main__":
    raise SystemExit(main())
