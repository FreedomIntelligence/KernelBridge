from dataclasses import replace
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from school_sdk.config import load_config
from school_sdk.ssh import ssh, ssh_ok


def cfg(user: str):
    return replace(load_config(), ssh_user=user, alias=f"school-{user}")


def run(user: str, cmd: str, timeout: int = 40):
    proc = ssh(cmd, cfg=cfg(user), timeout=timeout)
    print(f"===== {user} rc={proc.returncode} ok={ssh_ok(proc)} =====")
    print(proc.stdout or "")
    if proc.stderr:
        print("STDERR", proc.stderr[-300:])
    return proc


SITE = r"""
SP=/usr/local/python3.11.15/lib/python3.11/site-packages
STDLIB=/usr/local/python3.11.15/lib/python3.11
SHARE=/models/share/ka/cann-bench-20260722
SC='import os
os.environ["TORCH_DEVICE_BACKEND_AUTOLOAD"] = "0"
'
printf '%s\n' "$SC" > "$SP/sitecustomize.py"
printf '%s\n' "$SC" > "$STDLIB/sitecustomize.py"
mkdir -p "$SHARE/src"
printf '%s\n' "$SC" > "$SHARE/src/sitecustomize.py"
python3 -c 'import sitecustomize,os; print("SITE", sitecustomize.__file__, os.environ.get("TORCH_DEVICE_BACKEND_AUTOLOAD"))'
"""

BENCH1 = r"""
SHARE=/models/share/ka/cann-bench-20260722
HOME_BENCH=/home/demo1/cann-bench-20260722
BAK=/home/demo1/cann-bench-20260722.bak-20260824
[ -d "$SHARE/scripts" ] || { echo MISS_SHARE; exit 2; }
if [ -L "$HOME_BENCH" ]; then
  ln -sfn "$SHARE" "$HOME_BENCH"
  echo ALREADY_LINK
elif [ -d "$HOME_BENCH" ]; then
  if [ -e "$BAK" ]; then echo BAK_EXISTS; else mv "$HOME_BENCH" "$BAK"; echo MOVED_BAK; fi
  [ -e "$HOME_BENCH" ] && [ ! -L "$HOME_BENCH" ] && { echo STILL_DIR; exit 3; }
  ln -sfn "$SHARE" "$HOME_BENCH"
  echo LINKED
else
  mkdir -p /home/demo1
  ln -sfn "$SHARE" "$HOME_BENCH"
  echo NEW_LINK
fi
ls -ld "$HOME_BENCH"
readlink -f "$HOME_BENCH"
test -d "$HOME_BENCH/scripts" && echo SCRIPTS_OK
"""

TOOLKIT = r"""
echo '--- toolkit ---'
ls -ld /usr/local/Ascend/ascend-toolkit/latest /usr/local/Ascend/cann-9.0.0 2>&1
readlink -f /usr/local/Ascend/ascend-toolkit/latest 2>&1
echo '--- npu-smi / cann ---'
cat /usr/local/Ascend/cann-9.0.0/runtime/version.info 2>/dev/null | head -n 8
"""

print("sitecustomize both")
run("demo1", SITE)
run("demo2", SITE)
print("demo1 bench -> share")
run("demo1", BENCH1)
print("toolkit inspect")
run("demo1", TOOLKIT)
run("demo2", TOOLKIT)
