#!/usr/bin/env bash
# Zero-credit 3-case kernel-C probe for GroupedQueryAttn on demo1/demo2.
# Isolated /tmp compile — does not touch /models/share cmake (safe while KTP runs).
#
# Layered probe (do not start with 20):
#   hang-check:  --warmup 1 --repeat 1          (no --perf)
#   names:       --perf --warmup 1 --repeat 1
#   speed:       --perf --warmup 3 --repeat 8   same card, parent then arm
#
# usage:
#   gqa_dev_cases.sh [--perf] [--warmup N] [--repeat N] <source-dir> <dev-id> <case-ids>
#   GQA_SSH_USER=demo2 gqa_dev_cases.sh ...
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TEMPLATE="${GQA_TEMPLATE:-$HERE/../cann_submit/template}"
SSH_USER="${GQA_SSH_USER:-demo1}"
REMOTE_REPO="${GQA_REPO:-/models/share/ka/cann-bench-20260722}"
REMOTE_TASK="${PROBE_TASK:-level4/gqa}"
OPERATOR="${PROBE_OP:-GQA}"
OPDIR="${PROBE_OPDIR:-gqa}"
SSH=(ssh -F /dev/null -o BindInterface=en0 -o BatchMode=yes \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=20 \
  -p 31222 "${SSH_USER}@10.1.30.201")
SCP=(scp -F /dev/null -O -o BindInterface=en0 -o BatchMode=yes \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o ConnectTimeout=20 -P 31222)

PERF=0
WARMUP=3
REPEAT=5
while [[ $# -gt 0 ]]; do
  case "$1" in
    --perf) PERF=1; shift ;;
    --warmup) WARMUP="$2"; shift 2 ;;
    --repeat) REPEAT="$2"; shift 2 ;;
    *) break ;;
  esac
done
[[ $# == 3 ]] || {
  echo "usage: [--perf] [--warmup N] [--repeat N] <source-dir> <device-id> <case-ids>" >&2
  exit 2
}
SOURCE="$(cd "$1" && pwd -P)"
DEVICE="$2"
CASE_SPEC=${3//,/ }
read -r -a CASE_IDS <<<"$CASE_SPEC"
CASE_CSV=$(IFS=,; printf '%s' "${CASE_IDS[*]}")
[[ -d "$SOURCE/op_kernel" ]] || { echo "missing op_kernel in $SOURCE" >&2; exit 2; }

TAG="${GQA_FAM_TAG:-gqadev_$(date -u +%Y%m%dT%H%M%SZ)_$$_d${DEVICE}}"
RUN="/tmp/$TAG"
ARCHIVE="/tmp/$TAG.tgz"

stage=$(mktemp -d)
cp -R "$TEMPLATE" "$stage/source"
find "$stage/source/csrc/ops" -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +
cp -R "$SOURCE" "$stage/source/csrc/ops/$OPDIR"
HELPER="$HERE/gqa_family_helper.py"
ANCHORS="$HERE/gqa_official_anchors.json"
if [[ "$OPERATOR" == "GQA" ]]; then
  [[ -s "$HELPER" ]] || { echo "missing $HELPER" >&2; exit 2; }
  cp "$HELPER" "$stage/gqa_family_helper.py"
  [[ -s "$ANCHORS" ]] && cp "$ANCHORS" "$stage/gqa_official_anchors.json"
fi
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
tar czf "$stage/source.tgz" -C "$stage" source \
  $([ -f "$stage/gqa_family_helper.py" ] && echo gqa_family_helper.py) \
  $([ -f "$stage/gqa_official_anchors.json" ] && echo gqa_official_anchors.json)
copied=0
for attempt in 1 2 3; do
  "${SCP[@]}" "$stage/source.tgz" "${SSH_USER}@10.1.30.201:$ARCHIVE" >/dev/null 2>&1 || true
  probe=$("${SSH[@]}" "test -s '$ARCHIVE' && printf COPY_OK" 2>/dev/null || true)
  if [[ "$probe" == *COPY_OK* ]]; then copied=1; break; fi
done
rm -rf "$stage"
[[ "$copied" == 1 ]] || { echo "source upload failed after 3 attempts" >&2; exit 4; }

echo "GQA probe tag=$TAG host=$SSH_USER dev=$DEVICE cases=$CASE_CSV perf=$PERF w=$WARMUP r=$REPEAT"

"${SSH[@]}" bash -s -- "$RUN" "$ARCHIVE" "$REMOTE_REPO" "$REMOTE_TASK" "$OPERATOR" \
  "$DEVICE" "$PERF" "$CASE_CSV" "$WARMUP" "$REPEAT" <<'REMOTE'
set -u
RUN=$1; ARCHIVE=$2; REPO=$3; TASK_REL=$4; OP=$5; DEV=$6; PERF=$7; CASE_CSV=$8; WARMUP=$9; REPEAT=${10}
export RUN
echo "MARK unpack"
rm -rf "$RUN"; mkdir -p "$RUN"; tar xzf "$ARCHIVE" -C "$RUN" || exit 5
# run_evaluation prepends $REPO/src to PYTHONPATH, which would shadow site-packages
# sitecustomize. eval-child then `import torch` (autoload npu) + `import torch_npu`.
printf '%s\n' 'import os' 'os.environ["TORCH_DEVICE_BACKEND_AUTOLOAD"] = "0"' \
  > "$REPO/src/sitecustomize.py"
TD="$RUN/task/$TASK_REL"; mkdir -p "$(dirname "$TD")"
cp -R "$REPO/tasks/$TASK_REL" "$TD"; chmod -R u+w "$TD"
echo "MARK cases $CASE_CSV"
python3 - "$TD/cases.yaml" "$CASE_CSV" <<'PY'
import json, os, sys, yaml
from pathlib import Path
p = Path(sys.argv[1]); want = [int(x) for x in sys.argv[2].split(',')]
d = yaml.safe_load(p.read_text()); by = {int(x['case_id']): x for x in d['cases']}
missing = [x for x in want if x not in by]
if missing:
    raise SystemExit(f"missing case ids {missing}")
anchors = {}
run = Path(os.environ.get("RUN") or ".")
for ap in (run / "gqa_official_anchors.json",
           Path("/models/share/ka/cann_campaign/r950_tools/gqa_official_anchors.json")):
    if ap.is_file():
        anchors = (json.loads(ap.read_text()).get("cases") or {})
        break
for cid in want:
    c = by[cid]
    a = anchors.get(str(cid)) or anchors.get(cid)
    if a:
        c["baseline_perf_us"] = float(a["B"])
        c["t_hw_us"] = float(a["H"])
d['cases'] = [by[x] for x in want]
q = p.with_suffix('.tmp')
q.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
os.replace(q, p)
print("kept", [int(c['case_id']) for c in d['cases']],
      "anchored", sum(1 for c in d['cases'] if c.get("baseline_perf_us")))
PY
echo "MARK venv"
python3 -m venv --system-site-packages "$RUN/venv"
# torch 2.10 autoloads torch_npu; workers then import it again ("npu and npu").
# sitecustomize is inherited by staged_eval children; a shell export is not enough.
VSP="$("$RUN/venv/bin/python" -c "import sysconfig; print(sysconfig.get_path('purelib'))")"
printf '%s\n' 'import os' 'os.environ["TORCH_DEVICE_BACKEND_AUTOLOAD"] = "0"' > "$VSP/sitecustomize.py"
"$RUN/venv/bin/pip" install -q build wheel setuptools >/dev/null 2>&1 || \
  "$RUN/venv/bin/pip" install build wheel setuptools >>"$RUN/venv_pip.log" 2>&1 || true
"$RUN/venv/bin/python" -c "import build" || { echo "MARK missing python -m build"; cat "$RUN/venv_pip.log" 2>/dev/null | tail -30; exit 6; }
# Wrap after pip: eval-child is `[sys.executable, -m kernel_eval.cli]`.
# venv/bin/python -> python3 -> system CPython, so wrapping only python3.11
# never ran. Force AUTOLOAD=0 on every launcher.
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
# torch 2.10 autoloads torch_npu; residue from 2.13+cu130 makes that path hang.
# functorch shim lives in system site-packages (venv --system-site-packages).
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
ARGS=(./scripts/run_evaluation.sh --source-dir "$RUN/source" --task-dir "$RUN/task" \
  --operator "$OP" --device npu --device-id "$DEV" --reports-dir "$RUN/reports" \
  --warmup "$WARMUP" --repeat "$REPEAT")
[[ "$PERF" == 1 ]] || ARGS+=(--no-perf)
echo "MARK eval start $(date +%T) perf=$PERF w=$WARMUP r=$REPEAT"
(
  export PATH="$RUN/venv/bin:$PATH" PIP_FORCE_REINSTALL=1 CANN_BUILD_JOBS="${CANN_BUILD_JOBS:-12}"
  export ASCEND_HOME_PATH="$C9" NPU_ARCH=ascend910_93
  export LD_LIBRARY_PATH="$C9/lib64:${LD_LIBRARY_PATH:-}"
  export TORCH_DEVICE_BACKEND_AUTOLOAD=0
  cd "$REPO"; "${ARGS[@]}"
) >"$RUN/evaluation.log" 2>&1
rc=$?
echo "MARK eval done rc=$rc $(date +%T)"
if grep -E '507015|AI CORE EXCEPTION|core dump' "$RUN/evaluation.log" >/dev/null 2>&1; then
  echo "MARK HANG_OR_507015"
  grep -E '507015|AI CORE EXCEPTION|core dump' "$RUN/evaluation.log" | tail -20
fi
if [[ -f "$RUN/gqa_family_helper.py" ]]; then
  python3 "$RUN/gqa_family_helper.py" report "$RUN" "$PERF"
else
  python3 - "$RUN/reports" "$RUN/evaluation.log" <<'PY'
import glob, json, os, sys
r, l = sys.argv[1:]
fs = glob.glob(r+'/cann_final_eval*.json') or glob.glob(r+'/cann_correctness_eval*.json')
if not fs:
    print('NO_REPORT'); print(open(l, errors='ignore').read()[-8000:]); raise SystemExit(1)
d = json.load(open(max(fs, key=os.path.getmtime)))
for o in d.get('operators') or []:
    for c in o.get('cases') or []:
        a = c.get('accuracy') or {}
        print('CASE', c.get('case_id'), 'passed=', a.get('passed'),
              'us=', c.get('elapsed_us'), 'err=', a.get('error_message') or c.get('error_message'))
    print('SUMMARY', o.get('passed_cases'), '/', o.get('total_cases'), 'score=', o.get('score'))
PY
fi
echo "RUN_DIR=$RUN EVAL_RC=$rc"
exit $rc
REMOTE
