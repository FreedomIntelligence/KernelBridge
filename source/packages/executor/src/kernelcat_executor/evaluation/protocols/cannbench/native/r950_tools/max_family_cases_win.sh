#!/usr/bin/env bash
# Zero-credit CUSTOM-case gate for Maximum on demo1/demo2.
# Isolated /tmp compile — does not touch /models/share cmake (safe while KTP runs).
#
# Layered probe (do not start with 20):
#   hang-check:  --warmup 1 --repeat 1          (no --perf)
#   names:       --perf --warmup 1 --repeat 1
#   speed:       --perf --warmup 3 --repeat 8   same card, parent then arm
#
# usage:
#   max_family_cases.sh [--perf] [--warmup N] [--repeat N] <source-dir> <dev-id> <cases-json>
#   MAX_SSH_USER=demo2 max_family_cases.sh ...
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TEMPLATE="${MAX_TEMPLATE:-$HERE/../cann_submit/template}"
SSH_USER="${MAX_SSH_USER:-demo1}"
REMOTE_REPO="${MAX_REPO:-/home/${SSH_USER}/cann-bench-20260722}"
REMOTE_TASK=level2/maximum
OPERATOR=Maximum
HELPER="$HERE/max_family_helper.py"
[[ -s "$HELPER" ]] || { echo "missing $HELPER" >&2; exit 2; }
SSH=(ssh -o BatchMode=yes \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=20 \
  -p 31222 "${SSH_USER}@10.1.30.201")
SCP=(scp -O -o BatchMode=yes \
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
  echo "usage: [--perf] [--warmup N] [--repeat N] <source-dir> <device-id> <cases-json>" >&2
  exit 2
}
SOURCE="$(cd "$1" && pwd -P)"
DEVICE=$2
CASES_JSON="$3"
[[ -d "$SOURCE/op_kernel" ]] || { echo "missing op_kernel in $SOURCE" >&2; exit 2; }
[[ -s "$CASES_JSON" ]] || { echo "cases json missing: $CASES_JSON" >&2; exit 2; }
python - "$CASES_JSON" <<'PY'
import json, sys
cs = json.load(open(sys.argv[1]))
assert isinstance(cs, list) and cs, "cases json must be a non-empty list"
for c in cs:
    for k in ("attrs", "case_id", "dtype", "input_shape", "operator", "value_range"):
        assert k in c, f"case {c.get('case_id')} missing {k}"
print(f"cases ok: {len(cs)}")
PY
CASES_B64=$(base64 < "$CASES_JSON" | tr -d '\n')
TAG="${MAX_FAM_TAG:-maxfam_$(date -u +%Y%m%dT%H%M%SZ)_$$_d${DEVICE}}"
RUN="/tmp/$TAG"
ARCHIVE="/tmp/$TAG.tgz"

stage=$(mktemp -d)
cp -R "$TEMPLATE" "$stage/source"
find "$stage/source/csrc/ops" -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +
cp -R "$SOURCE" "$stage/source/csrc/ops/maximum"
cp "$HELPER" "$stage/max_family_helper.py"
find "$stage" -name '._*' -delete
# win: autocrlf=true 会让 .sh 带 CRLF，远端 bash 报语法错 —— 打包前统一剥 \r
find "$stage" -type f -name '*.sh' -exec sed -i 's/\r$//' {} +
python - "$stage/source/setup.py" <<'PY'
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
tar czf "$stage/source.tgz" -C "$stage" source max_family_helper.py
copied=0
for attempt in 1 2 3; do
  "${SCP[@]}" "$stage/source.tgz" "${SSH_USER}@10.1.30.201:$ARCHIVE" >/dev/null 2>&1 || true
  probe=$("${SSH[@]}" "test -s '$ARCHIVE' && printf COPY_OK" 2>/dev/null || true)
  if [[ "$probe" == *COPY_OK* ]]; then copied=1; break; fi
done
rm -rf "$stage"
[[ "$copied" == 1 ]] || { echo "source upload failed after 3 attempts" >&2; exit 4; }

echo "MAX probe tag=$TAG host=$SSH_USER dev=$DEVICE perf=$PERF w=$WARMUP r=$REPEAT src=$SOURCE"

"${SSH[@]}" bash -s -- "$REMOTE_REPO" "$REMOTE_TASK" "$OPERATOR" "$DEVICE" \
  "$PERF" "$WARMUP" "$REPEAT" "$RUN" "$ARCHIVE" "$CASES_B64" <<'REMOTE'
set -u
REPO=$1; TASK_REL=$2; OP=$3; DEV=$4; PERF=$5; WARMUP=$6; REPEAT=$7; RUN=$8; ARCHIVE=$9; CASES_B64=${10}
echo "MARK unpack host=$(whoami) run=$RUN"
rm -rf "$RUN"; mkdir -p "$RUN"; tar xzf "$ARCHIVE" -C "$RUN" || exit 5
TD="$RUN/task/$TASK_REL"; mkdir -p "$(dirname "$TD")"
cp -R "$REPO/tasks/$TASK_REL" "$TD"; chmod -R u+w "$TD"
printf '%s' "$CASES_B64" | base64 -d > "$RUN/cases.json"
python3 - "$TD/cases.yaml" "$RUN/cases.json" <<'PY'
import json, os, sys, yaml
from pathlib import Path
p = Path(sys.argv[1])
cases = json.load(open(sys.argv[2]))
d = yaml.safe_load(p.read_text())
d['cases'] = cases
q = p.with_suffix('.tmp')
q.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
os.replace(q, p)
print("kept", [int(c['case_id']) for c in d['cases']])
PY
python3 -m venv --system-site-packages "$RUN/venv"
# Hard-pin cann-9.0.0. A leaked ASCEND_HOME_PATH (toolkit/latest) drops Compares.
C9=/usr/local/Ascend/cann-9.0.0
C9S="$RUN/cann9"; mkdir -p "$C9S/runtime"
for e in "$C9"/*; do b=$(basename "$e"); [[ "$b" == runtime ]] || ln -s "$e" "$C9S/$b"; done
for e in "$C9/runtime"/*; do b=$(basename "$e"); [[ "$b" == version.info ]] || ln -s "$e" "$C9S/runtime/$b"; done
cp /usr/local/Ascend/ascend-toolkit/latest/runtime/version.info "$C9S/runtime/version.info"
ARGS=(./scripts/run_evaluation.sh --source-dir "$RUN/source" --task-dir "$RUN/task" \
  --operator "$OP" --device npu --device-id "$DEV" --reports-dir "$RUN/reports" \
  --warmup "$WARMUP" --repeat "$REPEAT")
[[ "$PERF" == 1 ]] || ARGS+=(--no-perf)
echo "MARK eval start $(date +%T) perf=$PERF w=$WARMUP r=$REPEAT"
(
 export PATH="$RUN/venv/bin:$PATH" PIP_FORCE_REINSTALL=1 CANN_BUILD_JOBS="${CANN_BUILD_JOBS:-12}"
 export ASCEND_HOME_PATH="$C9S"
 export LD_LIBRARY_PATH="${C9}/lib64:${LD_LIBRARY_PATH:-}"
 export TORCH_DEVICE_BACKEND_AUTOLOAD=0
 cd "$REPO"; "${ARGS[@]}"
) >"$RUN/evaluation.log" 2>&1
rc=$?
echo "MARK eval done rc=$rc $(date +%T)"
if grep -E '507015|507034|AI CORE EXCEPTION|core dump' "$RUN/evaluation.log" >/dev/null 2>&1; then
  echo "MARK HANG_OR_507015"
  grep -E '507015|507034|AI CORE EXCEPTION|core dump' "$RUN/evaluation.log" | tail -20
fi
python3 "$RUN/max_family_helper.py" report "$RUN" "$PERF"
echo "RUN_DIR=$RUN EVAL_RC=$rc"
exit $rc
REMOTE
