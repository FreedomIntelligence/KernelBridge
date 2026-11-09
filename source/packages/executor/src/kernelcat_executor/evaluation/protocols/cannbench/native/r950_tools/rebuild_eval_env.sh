#!/usr/bin/env bash
# Rebuild one 16-card eval host (demo1 or demo2) to the known-good 910C stack:
#   Python 3.11.15 + torch 2.10.0+cpu + torch_npu 2.10.0.post2
#   CANN 9.0.0, cann-bench from /models/share/ka, TORCH_DEVICE_BACKEND_AUTOLOAD=0
#
# usage (on the host, as root via demo1/demo2 ssh):
#   bash rebuild_eval_env.sh demo1|demo2
set -u
NAME="${1:?usage: rebuild_eval_env.sh demo1|demo2}"
SP=/usr/local/python3.11.15/lib/python3.11/site-packages
STDLIB=/usr/local/python3.11.15/lib/python3.11
TAR=/models/share/ka/ext/torch_npu_py311.tgz
SHARE_BENCH=/models/share/ka/cann-bench-20260722
HOME_BENCH="/home/${NAME}/cann-bench-20260722"
PIP_IDX="${PIP_IDX:-https://repo.huaweicloud.com/repository/pypi/simple}"

echo "MARK host=$NAME $(date -u +%Y%m%dT%H%M%SZ)"
echo "MARK stop leftover kernel_eval"
pkill -9 -f '[k]ernel_eval' >/dev/null 2>&1 || true
pkill -9 -f 'staged_eval' >/dev/null 2>&1 || true
sleep 2
echo "KE=$(ps -eo command 2>/dev/null | grep -c '[k]ernel_eval' || true)"

[[ -s "$TAR" ]] || { echo "⛔ missing pack $TAR"; exit 2; }
[[ -d "$SHARE_BENCH/scripts" ]] || { echo "⛔ missing share bench $SHARE_BENCH"; exit 2; }
[[ -d "$SP" ]] || { echo "⛔ missing site-packages $SP"; exit 2; }

echo "MARK wipe conflicting torch/triton"
cd "$SP" || exit 2
rm -rf torch torch-*.dist-info functorch \
  torch_npu torch_npu-*.dist-info \
  torchgen torchnpugen \
  triton triton-*.dist-info

echo "MARK extract pack $(stat -c %s "$TAR")B"
tar xzf "$TAR" -C "$SP" || { echo "⛔ tar extract failed"; exit 3; }

echo "MARK functorch shim from torch._functorch"
python3 - <<'PY'
from pathlib import Path
p = Path("/usr/local/python3.11.15/lib/python3.11/site-packages/functorch")
p.mkdir(exist_ok=True)
(p / "compile.py").write_text(
    "from torch._functorch.compilers import *\n"
    "from torch._functorch.partitioners import *\n"
    "from torch._functorch.aot_autograd import *\n"
)
(p / "__init__.py").write_text("from .compile import *\n")
print("FUNCTORCH_SHIM", p)
PY

echo "MARK sitecustomize AUTOLOAD=0"
SC='import os
os.environ["TORCH_DEVICE_BACKEND_AUTOLOAD"] = "0"
'
printf '%s\n' "$SC" > "$SP/sitecustomize.py"
printf '%s\n' "$SC" > "$STDLIB/sitecustomize.py"
mkdir -p "$SHARE_BENCH/src"
printf '%s\n' "$SC" > "$SHARE_BENCH/src/sitecustomize.py"

echo "MARK pip build/wheel/setuptools"
python3 -m pip install -q -i "$PIP_IDX" build wheel setuptools >/dev/null 2>&1 \
  || python3 -m pip install -i "$PIP_IDX" build wheel setuptools || true
python3 -c "import build,wheel,setuptools; print('BUILD_OK', build.__file__)" \
  || echo "⚠ python -m build still missing (venv must pip install it)"

echo "MARK bench symlink $HOME_BENCH -> $SHARE_BENCH"
mkdir -p "/home/${NAME}"
ln -sfn "$SHARE_BENCH" "$HOME_BENCH"
ls -ld "$HOME_BENCH"

echo "MARK verify"
export TORCH_DEVICE_BACKEND_AUTOLOAD=0
python3 - <<'PY'
import os, sys
os.environ["TORCH_DEVICE_BACKEND_AUTOLOAD"] = "0"
import sitecustomize
print("sitecustomize", sitecustomize.__file__)
print("AUTOLOAD", os.environ.get("TORCH_DEVICE_BACKEND_AUTOLOAD"))
import torch
print("torch", torch.__version__, "npu_attr", hasattr(torch, "npu"))
import torch_npu
n = int(torch.npu.device_count())
print("torch_npu", getattr(torch_npu, "__version__", "?"), "npu_count", n)
if n < 1:
    sys.exit(4)
print("VERIFY_OK")
PY
rc=$?
echo "DAVINCI=$(ls /dev/davinci[0-9]* 2>/dev/null | wc -l)"
echo "KE=$(ps -eo command 2>/dev/null | grep -c '[k]ernel_eval' || true)"
echo "CANN=$(ls -d /usr/local/Ascend/cann-9.0.0 2>/dev/null || echo missing)"
echo "MARK done rc=$rc"
exit $rc
