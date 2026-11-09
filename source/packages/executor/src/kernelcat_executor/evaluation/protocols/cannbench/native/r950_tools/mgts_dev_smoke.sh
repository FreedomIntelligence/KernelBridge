#!/usr/bin/env bash
# Zero-credit synthetic smoke for MoeGatingTopKSoftmax (mgts_debtfix_v1) on demo2.
# Cloned from cel_soft_dev_smoke.sh.  Difference: replaces the copied task's
# cases.yaml with SYNTHETIC cases aimed at the 6 hidden-debt signatures
# (job_647ca1fdafa4): 71/72 (E<8 perf_small regression family), 28/41 (cancel
# bucket), 29 (fp32 systematic), 66 (NaN position).
#
# usage: mgts_dev_smoke.sh <source-dir> <dev-id>
#   MGTS_SSH_USER=demo2 mgts_dev_smoke.sh ... (default demo2; demo1 only if free)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TEMPLATE="${MGTS_TEMPLATE:-$HERE/../cann_submit/template}"
SSH_USER="${MGTS_SSH_USER:-demo2}"
REMOTE_REPO="${MGTS_REPO:-/models/share/ka/cann-bench-20260722}"
REMOTE_TASK=level3/moe_gating_top_k_softmax
OPERATOR=MoeGatingTopKSoftmax
OP_DIR=moe_gating_top_k_softmax
SSH=(ssh -F /dev/null -o BindInterface=en0 -o BatchMode=yes \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=20 \
  -p 31222 "${SSH_USER}@10.1.30.201")
SCP=(scp -F /dev/null -O -o BindInterface=en0 -o BatchMode=yes \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o ConnectTimeout=20 -P 31222)

[[ $# == 2 ]] || { echo "usage: <source-dir> <dev-id>" >&2; exit 2; }
SOURCE="$(cd "$1" && pwd -P)"
DEVICE="$2"
[[ -d "$SOURCE/op_kernel" ]] || { echo "missing op_kernel in $SOURCE" >&2; exit 2; }

TAG="${MGTS_TAG:-mgtssmoke_$(date -u +%Y%m%dT%H%M%SZ)_$$_d${DEVICE}}"
RUN="/tmp/$TAG"
ARCHIVE="/tmp/$TAG.tgz"
HELPER="$HERE/mgts_smoke_helper.py"
[[ -s "$HELPER" ]] || { echo "missing $HELPER" >&2; exit 2; }

stage=$(mktemp -d)
cp -R "$TEMPLATE" "$stage/source"
find "$stage/source/csrc/ops" -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +
cp -R "$SOURCE" "$stage/source/csrc/ops/$OP_DIR"
cp "$HELPER" "$stage/mgts_smoke_helper.py"
find "$stage" -name '._*' -delete
python3 "$HERE/gen_init.py" "$stage/source"
python3 - "$stage/source/setup.py" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
s = p.read_text()
old = "cpu_count = os.cpu_count() or 2"
new = 'cpu_count = int(os.environ.get("CANN_BUILD_JOBS") or (os.cpu_count() or 2))'
if s.count(old) != 1:
    raise SystemExit(f"setup.py anchor count={s.count(old)}")
p.write_text(s.replace(old, new))
PY
tar czf "$stage/source.tgz" -C "$stage" source mgts_smoke_helper.py
copied=0
for attempt in 1 2 3; do
  "${SCP[@]}" "$stage/source.tgz" "${SSH_USER}@10.1.30.201:$ARCHIVE" >/dev/null 2>&1 || true
  probe=$("${SSH[@]}" "test -s '$ARCHIVE' && printf COPY_OK" 2>/dev/null || true)
  if [[ "$probe" == *COPY_OK* ]]; then copied=1; break; fi
done
rm -rf "$stage"
[[ "$copied" == 1 ]] || { echo "source upload failed after 3 attempts" >&2; exit 4; }

echo "MGTS smoke tag=$TAG host=$SSH_USER dev=$DEVICE (no-perf)"

"${SSH[@]}" "MGTS_CANN='${MGTS_CANN:-}' bash -s -- '$RUN' '$ARCHIVE' '$REMOTE_REPO' '$REMOTE_TASK' '$OPERATOR' '$DEVICE'" <<'REMOTE'
set -u
RUN=$1; ARCHIVE=$2; REPO=$3; TASK_REL=$4; OP=$5; DEV=$6
export RUN
# CANN9 环境(抄 ab910.sh AB_CANN=9 自愈块, 2026-08-21 定案): 裸 ssh 的 PATH 带
# 8.3.RC1/latest ⇒ bench 编译器解析到 8.3 bisheng ⇒ AscendC::Compares 编不过(本 smoke
# 首跑实证 0/8); 裸指 cann-9.0.0 又缺 runtime/version.info ⇒ torch_npu import 崩。
# /tmp/cann9_ab = cann-9.0.0 符号链接农场 + 真 version.info + lib64 前置。
C9=/usr/local/Ascend/cann-9.0.0
VI_OK=0
[ -f /tmp/cann9_ab/runtime/version.info ] && \
  grep -q 'required_package_opp_version' /tmp/cann9_ab/runtime/version.info && VI_OK=1
if [ "$VI_OK" != 1 ]; then
  rm -rf /tmp/cann9_ab; mkdir -p /tmp/cann9_ab
  for e in $C9/*; do b=$(basename "$e"); [ "$b" = "runtime" ] || ln -s "$e" /tmp/cann9_ab/"$b"; done
  mkdir -p /tmp/cann9_ab/runtime
  for e in $C9/runtime/*; do ln -s "$e" /tmp/cann9_ab/runtime/"$(basename "$e")"; done
  VI=""
  for c in \
    /usr/local/Ascend/ascend-toolkit/latest/runtime/version.info \
    "$C9/runtime/version.info"; do
    if [ -f "$c" ] && grep -q 'required_package_opp_version' "$c"; then VI=$c; break; fi
  done
  [ -n "$VI" ] && cp "$VI" /tmp/cann9_ab/runtime/version.info
fi
export ASCEND_HOME_PATH=/tmp/cann9_ab
export ASCEND_OPP_PATH=/tmp/cann9_ab/opp
export LD_LIBRARY_PATH=$C9/lib64:${LD_LIBRARY_PATH:-}
# PATH 洗掉 8.3/latest, 让 bench 编译器解析落到 9.0.0 工具链
export PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '8\.3\.RC1\|ascend-toolkit/latest' | paste -sd: -)
echo "MARK unpack"
rm -rf "$RUN"; mkdir -p "$RUN"; tar xzf "$ARCHIVE" -C "$RUN" || exit 5
# run_evaluation prepends $REPO/src to PYTHONPATH, which would shadow site-packages
# sitecustomize. eval-child then `import torch` (autoload npu) + `import torch_npu`.
printf '%s\n' 'import os' 'os.environ["TORCH_DEVICE_BACKEND_AUTOLOAD"] = "0"' \
  > "$REPO/src/sitecustomize.py"
TD="$RUN/task/$TASK_REL"; mkdir -p "$(dirname "$TD")"
cp -R "$REPO/tasks/$TASK_REL" "$TD"; chmod -R u+w "$TD"
echo "MARK synthetic cases 201..+"
python3 - "$TD/cases.yaml" <<'PY'
import sys, yaml
from pathlib import Path
p = Path(sys.argv[1])

cases = []
cid = 300

def add(shape, dt, k, vr, note):
    global cid
    cid += 1
    cases.append(dict(operator="MoeGatingTopKSoftmax", case_id=cid,
                      input_shape=[shape], dtype=[dt], attrs=dict(k=k),
                      value_range=[vr], note=note))

# ---- controls: public shapes that MUST pass (harness sanity) ----
add([1024, 16], "float16", 2, [-1, 1], "ctrl pub c1 fp16 E16 k2")
add([1024, 4], "float32", 2, [-0.1, 0.1], "ctrl pub c15 fp32 E4 k2 (perf_small fire)")
add([64, 8], "float16", 2, [-1, 1], "ctrl pub c14 fp16 E8 k2 (generic)")

# ---- group A: E<8 sweep (hidden 71/72 family; perf_small domain) ----
for dt in ("float16", "float32", "bfloat16"):
    for E in (1, 2, 4, 7):
        for k in sorted({1, E}):
            for outer in (1, 64):
                add([outer, E], dt, k, [-2, 2],
                    f"A {dt} E{E} k{k} outer{outer} r2")

# ---- group B: extreme value ranges on vecBatch/generic (28/29/41/66 family) ----
# fp16 vecBatch E in {16,32}
add([256, 16], "float16", 8, [-2, 2], "B fp16 E16 k8 o256 r2 ctrl")
add([256, 16], "float16", 8, [-1000, 1000], "B fp16 E16 k8 o256 r1e3")
add([256, 16], "float16", 8, [-65504, 65504], "B fp16 E16 k8 o256 r65504")
add([256, 16], "float16", 8, [-70000, 70000], "B fp16 E16 k8 o256 r70000(Inf)")
add([256, 32], "float16", 8, [-1000, 1000], "B fp16 E32 k8 o256 r1e3")
add([256, 32], "float16", 8, [-70000, 70000], "B fp16 E32 k8 o256 r70000(Inf)")
add([2048, 32], "float16", 1, [-65504, 65504], "B fp16 E32 k1 o2048 r65504 (c28-shape)")
add([2048, 32], "float16", 1, [-70000, 70000], "B fp16 E32 k1 o2048 r70000 (c28-shape Inf)")
add([1024, 16], "float16", 2, [-70000, 70000], "B fp16 E16 k2 o1024 r70000 (2048 elems)")
# bf16 vecBatch E in {16,32} + per-row E=511
add([256, 16], "bfloat16", 8, [-100, 100], "B bf16 E16 k8 o256 r100")
add([256, 16], "bfloat16", 8, [-1e38, 1e38], "B bf16 E16 k8 o256 r1e38")
add([256, 16], "bfloat16", 8, [-4e38, 4e38], "B bf16 E16 k8 o256 r4e38(Inf)")
add([511, 32], "bfloat16", 2, [-100, 100], "B bf16 E32 k2 o511 r100 (c66-shape 1022)")
add([511, 32], "bfloat16", 2, [-4e38, 4e38], "B bf16 E32 k2 o511 r4e38 (c66-shape Inf)")
add([1024, 32], "bfloat16", 8, [-100, 100], "B bf16 E32 k8 o1024 r100 (c41-shape 8192)")
add([1024, 32], "bfloat16", 8, [-4e38, 4e38], "B bf16 E32 k8 o1024 r4e38 (c41-shape Inf)")
add([1024, 511], "bfloat16", 8, [-100, 100], "B bf16 E511 k8 o1024 r100 (8192 per-row)")
# fp32 vecBatch E in {8,16,24,32} + per-row E=511
add([1, 4], "float32", 2, [-2, 2], "B fp32 E4 k2 o1 r2 (c72-shape 2 elems)")
add([2, 4], "float32", 1, [-2, 2], "B fp32 E4 k1 o2 r2 (2 elems)")
add([2, 2], "float32", 1, [-2, 2], "B fp32 E2 k1 o2 r2 (2 elems)")
add([1024, 16], "float32", 4, [-100, 100], "B fp32 E16 k4 o1024 r100 (c29-shape 4096)")
add([1024, 16], "float32", 4, [-1000, 1000], "B fp32 E16 k4 o1024 r1e3 (c29-shape)")
add([512, 32], "float32", 8, [-88, 88], "B fp32 E32 k8 o512 r88 (4096)")
add([512, 511], "float32", 8, [-100, 100], "B fp32 E511 k8 o512 r100 (4096 per-row)")
add([2, 8], "float32", 1, [-2, 2], "B fp32 E8 k1 o2 r2")
add([2, 8], "float32", 1, [-1000, 1000], "B fp32 E8 k1 o2 r1e3")

p.write_text(yaml.safe_dump({"cases": cases}, sort_keys=False, allow_unicode=True))
print("wrote", len(cases), "synthetic cases")
PY
echo "MARK venv"
python3 -m venv --system-site-packages "$RUN/venv"
VSP="$("$RUN/venv/bin/python" -c "import sysconfig; print(sysconfig.get_path('purelib'))")"
printf '%s\n' 'import os' 'os.environ["TORCH_DEVICE_BACKEND_AUTOLOAD"] = "0"' > "$VSP/sitecustomize.py"
"$RUN/venv/bin/pip" install -q build wheel setuptools >/dev/null 2>&1 || \
  "$RUN/venv/bin/pip" install build wheel setuptools >>"$RUN/venv_pip.log" 2>&1 || true
"$RUN/venv/bin/python" -c "import build" || { echo "MARK missing python -m build"; cat "$RUN/venv_pip.log" 2>/dev/null | tail -30; exit 6; }
VENV_BIN="$RUN/venv/bin"
REAL_PY=$(readlink -f "$VENV_BIN/python3" 2>/dev/null || readlink -f "$VENV_BIN/python")
if [[ -x "$REAL_PY" ]]; then
  ln -sfn "$REAL_PY" "$VENV_BIN/_cpython.real"
  printf '%s\n' '#!/bin/bash' \
    'export ASCEND_HOME_PATH="${ASCEND_HOME_PATH:-/usr/local/Ascend/cann-9.0.0}"' \
    'set +u' \
    'if [ -f "$ASCEND_HOME_PATH/bin/setenv.bash" ]; then' \
    '  source "$ASCEND_HOME_PATH/bin/setenv.bash" >/dev/null 2>&1 || true' \
    'fi' \
    'export ASCEND_OPP_PATH="${ASCEND_OPP_PATH:-$ASCEND_HOME_PATH/opp}"' \
    'export LD_LIBRARY_PATH="$ASCEND_HOME_PATH/lib64:${LD_LIBRARY_PATH:-}"' \
    'export TORCH_DEVICE_BACKEND_AUTOLOAD=0' \
    'exec "$(dirname "$0")/_cpython.real" "$@"' > "$VENV_BIN/_pywrap"
  chmod +x "$VENV_BIN/_pywrap"
  for _py in python python3 python3.11; do
    if [[ -e "$VENV_BIN/$_py" || -L "$VENV_BIN/$_py" ]]; then
      ln -sfn _pywrap "$VENV_BIN/$_py"
    fi
  done
fi
C9="${ASCEND_HOME_PATH:-/usr/local/Ascend/cann-9.0.0}"
set +u
source "$C9/bin/setenv.bash" >/dev/null 2>&1 || true
export TORCH_DEVICE_BACKEND_AUTOLOAD=0
export LD_LIBRARY_PATH="$C9/lib64:${LD_LIBRARY_PATH:-}"
set -u
echo "MARK cbu"
CBU_WORK="$RUN/cbu_src"
if [[ ! -d "$REPO/src/cann_bench_utils" ]]; then
  echo "MARK cbu SRC MISSING $REPO/src/cann_bench_utils"
  exit 6
fi
cp -R "$REPO/src/cann_bench_utils" "$CBU_WORK" || { echo "MARK cbu COPY FAILED"; exit 6; }
(
  cd "$CBU_WORK" || exit 6
  export TORCH_DEVICE_BACKEND_AUTOLOAD=0
  export ASCEND_HOME_PATH="$C9" ASCEND_OPP_PATH="$C9/opp"
  export LD_LIBRARY_PATH="$C9/lib64:${LD_LIBRARY_PATH:-}"
  PATH="$RUN/venv/bin:$PATH" PYTHON="$RUN/venv/bin/python" NPU_ARCH=ascend910_93 \
    bash build.sh --clean > "$RUN/cbu_build.log" 2>&1
  "$RUN/venv/bin/pip" install dist/cann_bench_utils-*.whl --force-reinstall --no-deps >> "$RUN/cbu_build.log" 2>&1
) || { echo "MARK cbu FAILED"; tail -80 "$RUN/cbu_build.log" 2>/dev/null || true; exit 6; }
TORCH_DEVICE_BACKEND_AUTOLOAD=0 "$RUN/venv/bin/python" -c "from cann_bench_utils import cann_bench_warmup, cann_bench_cache_clean" || \
  { echo "MARK cbu import FAILED"; tail -30 "$RUN/cbu_build.log"; exit 6; }
echo "MARK eval start $(date +%T) no-perf"
(
  export PATH="$RUN/venv/bin:$PATH" PIP_FORCE_REINSTALL=1 CANN_BUILD_JOBS="${CANN_BUILD_JOBS:-12}"
  export ASCEND_HOME_PATH="$C9" NPU_ARCH=ascend910_93
  export LD_LIBRARY_PATH="$C9/lib64:${LD_LIBRARY_PATH:-}"
  export TORCH_DEVICE_BACKEND_AUTOLOAD=0
  cd "$REPO"; ./scripts/run_evaluation.sh --source-dir "$RUN/source" --task-dir "$RUN/task" \
    --operator "$OP" --device npu --device-id "$DEV" --reports-dir "$RUN/reports" \
    --no-perf --warmup 1 --repeat 1
) >"$RUN/evaluation.log" 2>&1
rc=$?
echo "MARK eval done rc=$rc $(date +%T)"
if grep -E '507015|AI CORE EXCEPTION|core dump' "$RUN/evaluation.log" >/dev/null 2>&1; then
  echo "MARK HANG_OR_507015"
  grep -E '507015|AI CORE EXCEPTION|core dump' "$RUN/evaluation.log" | tail -20
fi
python3 "$RUN/mgts_smoke_helper.py" "$RUN"
echo "RUN_DIR=$RUN EVAL_RC=$rc"
exit $rc
REMOTE
