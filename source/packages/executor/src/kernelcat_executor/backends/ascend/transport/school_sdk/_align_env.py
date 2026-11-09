"""Compare / align demo1 and demo2 eval stacks via school_sdk paramiko."""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from school_sdk.config import load_config
from school_sdk.ssh import ssh, ssh_exec, ssh_ok

REBUILD = ROOT / "cann_910c_pipeline" / "r950_tools" / "rebuild_eval_env.sh"

FINGERPRINT = r"""
export TORCH_DEVICE_BACKEND_AUTOLOAD=0
echo USER=$(whoami)
echo PY=$(python3 -V 2>&1)
echo PYBIN=$(command -v python3)
python3 - <<'PY'
import os, sys
print("AUTO", os.environ.get("TORCH_DEVICE_BACKEND_AUTOLOAD"))
try:
    import sitecustomize
    print("SITECUSTOMIZE", getattr(sitecustomize, "__file__", "?"))
except Exception as e:
    print("SITECUSTOMIZE", type(e).__name__, e)
try:
    import torch
    print("TORCH", torch.__version__, "npu_attr", hasattr(torch, "npu"))
except Exception as e:
    print("TORCH", type(e).__name__, e)
try:
    import torch_npu
    import torch
    print("TORCH_NPU", getattr(torch_npu, "__version__", "?"), "npu_count", torch.npu.device_count())
except Exception as e:
    print("TORCH_NPU", type(e).__name__, e)
try:
    import build, wheel, setuptools
    print("BUILD_OK")
except Exception as e:
    print("BUILD", type(e).__name__, e)
PY
echo BENCH_LS=$(ls -ld /home/%NAME%/cann-bench-20260722 2>&1)
echo BENCH_REAL=$(readlink -f /home/%NAME%/cann-bench-20260722 2>/dev/null || echo none)
echo SHARE=$([ -d /models/share/ka/cann-bench-20260722/scripts ] && echo 1 || echo 0)
echo PACK=$(stat -c %s /models/share/ka/ext/torch_npu_py311.tgz 2>&1)
echo CANN9=$([ -d /usr/local/Ascend/cann-9.0.0 ] && echo 1 || echo 0)
echo TOOLKIT=$(readlink -f /usr/local/Ascend/ascend-toolkit/latest 2>/dev/null || echo none)
echo DAVINCI=$(ls /dev/davinci[0-9]* 2>/dev/null | wc -l)
echo KE=$(ps -eo command 2>/dev/null | grep -c '[k]ernel_eval' || true)
echo FINGERPRINT_OK
"""


def cfg_for(user: str):
    cfg = load_config()
    return replace(cfg, ssh_user=user, alias=f"school-{user}")


def run_user(user: str, cmd: str, timeout: int = 45, stdin: bytes | None = None):
    t0 = time.time()
    if stdin is None:
        proc = ssh(cmd, cfg=cfg_for(user), timeout=timeout)
    else:
        raw = ssh_exec(cmd, cfg=cfg_for(user), timeout=timeout, stdin=stdin)
        from school_sdk.ssh import _blob_text
        proc = type(raw)(
            args=raw.args,
            returncode=raw.returncode,
            stdout=_blob_text(raw.stdout),
            stderr=_blob_text(raw.stderr),
        )
    dt = time.time() - t0
    ok = ssh_ok(proc)
    print(f"--- {user} ok={ok} rc={proc.returncode} dt={dt:.1f}s ---")
    print(proc.stdout or "")
    if proc.stderr:
        print("STDERR", proc.stderr[-500:])
    return proc


def parse_fp(text: str) -> dict[str, str]:
    out = {}
    for line in (text or "").splitlines():
        if " " in line and not line.startswith("echo ") and line.split(" ", 1)[0] in {
            "AUTO", "SITECUSTOMIZE", "TORCH", "TORCH_NPU", "BUILD_OK", "BUILD",
        }:
            k, _, v = line.partition(" ")
            out[k] = v.strip()
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def needs_rebuild(fp: dict[str, str]) -> list[str]:
    why = []
    torch = fp.get("TORCH", "")
    npu = fp.get("TORCH_NPU", "")
    if "2.10.0+cpu" not in torch:
        why.append(f"torch={torch}")
    if "2.10.0.post2" not in npu and "npu_count" not in npu:
        why.append(f"torch_npu={npu}")
    if "npu_count 16" not in npu and "npu_count 16" != npu:
        # accept "2.10.0.post2 npu_count 16"
        if "npu_count 16" not in npu:
            why.append(f"npu={npu}")
    if fp.get("AUTO") not in ("0", "AUTO 0") and fp.get("AUTO") != "0":
        if "0" not in (fp.get("AUTO") or ""):
            why.append(f"autoload={fp.get('AUTO')}")
    if "sitecustomize" not in (fp.get("SITECUSTOMIZE") or "").lower() and "SITECUSTOMIZE" in fp:
        if "Error" in (fp.get("SITECUSTOMIZE") or "") or fp.get("SITECUSTOMIZE", "").startswith(("Module", "Import")):
            why.append(f"sitecustomize={fp.get('SITECUSTOMIZE')}")
    bench_real = fp.get("BENCH_REAL", "")
    if "/models/share/ka/cann-bench-20260722" not in bench_real:
        why.append(f"bench={fp.get('BENCH_LS')} -> {bench_real}")
    if fp.get("SHARE") != "1":
        why.append("share bench missing")
    if fp.get("CANN9") != "1":
        why.append("cann-9.0.0 missing")
    if fp.get("BUILD_OK") is None and "BUILD_OK" not in (fp.get("BUILD") or ""):
        if fp.get("BUILD"):
            why.append(f"build={fp.get('BUILD')}")
    return why


def rebuild(user: str) -> bool:
    if not REBUILD.is_file():
        raise SystemExit(f"missing {REBUILD}")
    remote = f"/tmp/rebuild_eval_env_{user}.sh"
    body = REBUILD.read_bytes().replace(b"\r\n", b"\n")
    print(f"put {REBUILD.name} -> {user}:{remote} bytes={len(body)}")
    put = run_user(user, f"cat > {remote} && echo PUT_OK", timeout=60, stdin=body)
    if "PUT_OK" not in (put.stdout or ""):
        print("put failed")
        return False
    print(f"run rebuild on {user} (may take a few minutes)")
    proc = run_user(user, f"bash {remote} {user}; echo REBUILD_RC=$?", timeout=300)
    return "VERIFY_OK" in (proc.stdout or "")


def main() -> int:
    fps = {}
    for user in ("demo1", "demo2"):
        cmd = FINGERPRINT.replace("%NAME%", user)
        proc = run_user(user, cmd, timeout=50)
        fps[user] = parse_fp(proc.stdout or "")
        fps[user]["_ok"] = "FINGERPRINT_OK" in (proc.stdout or "")
        fps[user]["_why"] = needs_rebuild(fps[user]) if fps[user]["_ok"] else ["probe failed"]

    print("===== diff =====")
    keys = [
        "PY", "TORCH", "TORCH_NPU", "AUTO", "SITECUSTOMIZE", "BUILD_OK", "BUILD",
        "BENCH_LS", "BENCH_REAL", "SHARE", "PACK", "CANN9", "TOOLKIT", "DAVINCI", "KE",
    ]
    for k in keys:
        a, b = fps["demo1"].get(k), fps["demo2"].get(k)
        mark = "OK" if a == b else "DIFF"
        print(f"{mark:4} {k:14} demo1={a!r}")
        print(f"     {k:14} demo2={b!r}")

    for user in ("demo1", "demo2"):
        print(f"{user} rebuild_needed={fps[user]['_why'] or 'no'} ke={fps[user].get('KE')}")

    do = "--apply" in sys.argv
    if not do:
        print("dry-run only; pass --apply to rebuild hosts that differ from gold pack")
        return 0

    failed = []
    for user in ("demo1", "demo2"):
        why = fps[user]["_why"]
        if not why:
            print(f"skip {user}: already gold")
            continue
        ke = fps[user].get("KE", "0")
        if ke not in ("0", "1", ""):  # grep -c of the probe command itself can be 1
            # treat >1 as live eval
            try:
                n = int(ke)
            except ValueError:
                n = 0
            if n > 1:
                print(f"skip {user}: kernel_eval={ke}, not killing a live eval")
                failed.append(user)
                continue
        if not rebuild(user):
            failed.append(user)
            continue
        proc = run_user(user, FINGERPRINT.replace("%NAME%", user), timeout=50)
        again = parse_fp(proc.stdout or "")
        leftover = needs_rebuild(again)
        print(f"{user} after={leftover or 'aligned'}")
        if leftover:
            failed.append(user)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
