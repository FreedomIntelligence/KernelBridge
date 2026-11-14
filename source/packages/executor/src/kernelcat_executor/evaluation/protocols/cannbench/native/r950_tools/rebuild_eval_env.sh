#!/usr/bin/env bash
# Rebuild an explicitly selected evaluation environment from an operator-supplied
# package archive. Run on the intended host after stopping its owned jobs.
set -eu
NAME="${1:-configured-host}"
PYTHON="${CANN_REBUILD_PYTHON:-python3}"
SP="$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
STDLIB="$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')"
TAR="${CANN_REBUILD_PACK:?Set CANN_REBUILD_PACK to the prepared package archive}"
SHARE_BENCH="${CANN_BENCH_SOURCE:?Set CANN_BENCH_SOURCE to the benchmark source}"
HOME_BENCH="${CANN_BENCH_HOME:?Set CANN_BENCH_HOME to the destination benchmark path}"
PIP_IDX="${PIP_IDX:-https://pypi.org/simple}"
export SP

echo "MARK host=$NAME $(date -u +%Y%m%dT%H%M%SZ)"
if pgrep -f '[k]ernel_eval|[s]taged_eval' >/dev/null; then
  echo "Evaluation processes are running; stop their owned jobs before rebuilding" >&2
  exit 3
fi

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
"$PYTHON" - <<'PY'
from pathlib import Path
import os
p = Path(os.environ["SP"]) / "functorch"
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
"$PYTHON" -m pip install -q -i "$PIP_IDX" build wheel setuptools >/dev/null 2>&1 \
  || "$PYTHON" -m pip install -i "$PIP_IDX" build wheel setuptools || true
"$PYTHON" -c "import build,wheel,setuptools; print('BUILD_OK', build.__file__)" \
  || echo "⚠ python -m build still missing (venv must pip install it)"

echo "MARK bench symlink $HOME_BENCH -> $SHARE_BENCH"
mkdir -p "$(dirname "$HOME_BENCH")"
ln -sfn "$SHARE_BENCH" "$HOME_BENCH"
ls -ld "$HOME_BENCH"

echo "MARK verify"
export TORCH_DEVICE_BACKEND_AUTOLOAD=0
"$PYTHON" - <<'PY'
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
