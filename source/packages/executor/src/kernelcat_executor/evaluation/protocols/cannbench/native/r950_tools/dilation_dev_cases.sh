#!/usr/bin/env bash
# Zero-credit selected-case gate for Dilation2D on the demo1 development host.
# Derived from c3d_dev_cases.sh; all remote traffic is pinned to en0 because
# the VPN route for 10.1.30.0/24 is not usable from this workstation.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TEMPLATE="$HERE/../cann_submit/template"
REMOTE_REPO="${DIL_REPO:-/models/share/ka/cann-bench-20260722}"
REMOTE_TASK=level3/dilation_2d
OPERATOR=Dilation2D
SSH=(ssh -F /dev/null -o BindInterface=en0 -o BatchMode=yes \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=20 \
  -p 31222 demo1@10.1.30.201)
SCP=(scp -F /dev/null -O -o BindInterface=en0 -o BatchMode=yes \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o ConnectTimeout=20 -P 31222)
if [[ -n "${DIL_SSH_CONTROL:-}" ]]; then
  SSH+=(-o "ControlPath=$DIL_SSH_CONTROL")
  SCP+=(-o "ControlPath=$DIL_SSH_CONTROL")
fi

PERF=0
[[ "${1:-}" == --perf ]] && { PERF=1; shift; }
[[ $# == 3 ]] || {
  echo "usage: dilation_dev_cases.sh [--perf] <source-dir> <device-id> <case-ids>" >&2
  exit 2
}
SOURCE="$(cd "$1" && pwd -P)"
DEVICE=$2
CASE_SPEC=${3//,/ }
read -r -a CASE_IDS <<<"$CASE_SPEC"
CASE_CSV=$(IFS=,; printf '%s' "${CASE_IDS[*]}")
TAG="dilation_$(date -u +%Y%m%dT%H%M%SZ)_$$_d${DEVICE}"
RUN="/tmp/$TAG"
ARCHIVE="/tmp/$TAG.tgz"

stage=$(mktemp -d)
cp -R "$TEMPLATE" "$stage/source"
find "$stage/source/csrc/ops" -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +
cp -R "$SOURCE" "$stage/source/csrc/ops/dilation_2d"
find "$stage" -name '._*' -delete
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
tar czf "$stage/source.tgz" -C "$stage" source
copied=0
for attempt in 1 2 3; do
  # The Go SSH gateway closes an otherwise successful legacy-scp transfer with
  # status -1/255.  Trusting scp's exit code therefore rejects a file that is
  # already complete on the host.  Verify the explicit remote target instead.
  "${SCP[@]}" "$stage/source.tgz" "demo1@10.1.30.201:$ARCHIVE" >/dev/null 2>&1 || true
  probe=$("${SSH[@]}" "test -s '$ARCHIVE' && printf COPY_OK" 2>/dev/null || true)
  if [[ "$probe" == *COPY_OK* ]]; then
    copied=1
    break
  fi
done
rm -rf "$stage"
[[ "$copied" == 1 ]] || { echo "source upload failed after 3 attempts" >&2; exit 4; }

"${SSH[@]}" bash -s -- "$REMOTE_REPO" "$REMOTE_TASK" "$OPERATOR" "$DEVICE" \
  "$CASE_CSV" "$PERF" "$RUN" "$ARCHIVE" <<'REMOTE'
set -u
REPO=$1; TASK_REL=$2; OP=$3; DEV=$4; CASE_CSV=$5; PERF=$6; RUN=$7; ARCHIVE=$8
rm -rf "$RUN"; mkdir -p "$RUN"; tar xzf "$ARCHIVE" -C "$RUN" || exit 5
TD="$RUN/task/$TASK_REL"; mkdir -p "$(dirname "$TD")"
cp -R "$REPO/tasks/$TASK_REL" "$TD"; chmod -R u+w "$TD"
python3 - "$TD/cases.yaml" "$CASE_CSV" <<'PY'
import os, sys, yaml
from pathlib import Path
p=Path(sys.argv[1]); want=[int(x) for x in sys.argv[2].split(',')]
d=yaml.safe_load(p.read_text()); by={int(x['case_id']):x for x in d['cases']}
d['cases']=[by[x] for x in want]
q=p.with_suffix('.tmp'); q.write_text(yaml.safe_dump(d,sort_keys=False,allow_unicode=True)); os.replace(q,p)
PY
python3 -m venv --system-site-packages "$RUN/venv"
C9=/usr/local/Ascend/cann-9.0.0; C9S="$RUN/cann9"; mkdir -p "$C9S/runtime"
for e in "$C9"/*; do b=$(basename "$e"); [[ "$b" == runtime ]] || ln -s "$e" "$C9S/$b"; done
for e in "$C9/runtime"/*; do b=$(basename "$e"); [[ "$b" == version.info ]] || ln -s "$e" "$C9S/runtime/$b"; done
cp /usr/local/Ascend/ascend-toolkit/latest/runtime/version.info "$C9S/runtime/version.info"
ARGS=(./scripts/run_evaluation.sh --source-dir "$RUN/source" --task-dir "$RUN/task" \
  --operator "$OP" --device npu --device-id "$DEV" --reports-dir "$RUN/reports")
[[ "$PERF" == 1 ]] || ARGS+=(--no-perf)
(
 export PATH="$RUN/venv/bin:$PATH" PIP_FORCE_REINSTALL=1 CANN_BUILD_JOBS=96
 export ASCEND_HOME_PATH="$C9S"
 export LD_LIBRARY_PATH="/usr/local/Ascend/cann-9.0.0/lib64:${LD_LIBRARY_PATH:-}"
 export TORCH_DEVICE_BACKEND_AUTOLOAD=0
 printf '%s\n' 'import os' 'os.environ["TORCH_DEVICE_BACKEND_AUTOLOAD"] = "0"' \
   > "$REPO/src/sitecustomize.py"
 cd "$REPO"; "${ARGS[@]}"
) >"$RUN/evaluation.log" 2>&1
rc=$?
python3 - "$RUN/reports" "$RUN/evaluation.log" <<'PY'
import glob,json,os,sys
r,l=sys.argv[1:]
fs=glob.glob(r+'/cann_final_eval*.json') or glob.glob(r+'/cann_correctness_eval*.json')
if not fs:
 print('NO_REPORT'); print(open(l,errors='ignore').read()[-12000:]); raise SystemExit(1)
d=json.load(open(max(fs,key=os.path.getmtime)))
for o in d.get('operators') or []:
 for c in o.get('cases') or []:
  a=c.get('accuracy') or {}
  print('CASE',c.get('case_id'),'passed=',a.get('passed'),'us=',c.get('elapsed_us'),'err=',a.get('error_message') or c.get('error_message'))
 print('SUMMARY',o.get('passed_cases'),'/',o.get('total_cases'),'score=',o.get('score'))
PY
echo "RUN_DIR=$RUN EVAL_RC=$rc"
exit $rc
REMOTE
