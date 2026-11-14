#!/usr/bin/env bash
# Run source-bound A/B measurements on an explicitly configured Ascend host.
# Usage: ab910.sh OP REPEATS ARM:SOURCE [ARM:SOURCE ...]
# Set AB_LOCAL=1 for local execution, otherwise set AB_SSH_HOST and optionally
# AB_SSH_USER / AB_SSH_PORT. CANN_BENCH_HOME is required in both modes.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$HERE/../cann_submit/template"
# GNU stat: -f is --file-system (prints overlayfs dump, exit 0). Darwin: -f %z is size.
# AB_LOCAL on KTP is Linux, so -f must NOT win. Prefer -c %s.
bytes_of() {
  if stat -c %s "$1" >/dev/null 2>&1; then
    stat -c %s "$1"
  else
    stat -f %z "$1"
  fi
}
SSH_IFACE_OPTS=()
if [ -n "${AB_SSH_INTERFACE:-}" ]; then
  SSH_IFACE_OPTS=(-o "BindInterface=${AB_SSH_INTERFACE}")
fi
AB_SSH_USER="${AB_SSH_USER:-}"
AB_SSH_HOST="${AB_SSH_HOST:-}"
AB_SSH_PORT="${AB_SSH_PORT:-22}"
AB_SSH="${AB_SSH_HOST}"
if [ -n "$AB_SSH_USER" ]; then AB_SSH="${AB_SSH_USER}@${AB_SSH_HOST}"; fi
DEV=(ssh "${SSH_IFACE_OPTS[@]}" -o BatchMode=yes \
  -o ServerAliveInterval=5 -o ServerAliveCountMax=4 \
  -o ConnectTimeout=20 -p "${AB_SSH_PORT}" "${AB_SSH}")

# Windows（OpenSSH 传输/交互挂死）：远端命令与上传改走 school_xfer（Paramiko）。
XFER_MODE=0
if [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]]; then
  XFER_MODE=1
  XFER="$(cygpath -w "$HERE/school_xfer.py")"
  DEV=(env MSYS2_ARG_CONV_EXCL='*' python "$XFER" exec 120)
fi

OP="${1:?用法: ab910.sh <算子> <重复次数> <臂名>:<源码目录>...}"; shift
REP="${1:?缺重复次数}"; shift
[ $# -ge 1 ] || { echo "至少给一个臂" >&2; exit 2; }

AB_LOCAL="${AB_LOCAL:-0}"
CANN_BENCH_HOME="${CANN_BENCH_HOME:?Set CANN_BENCH_HOME to the benchmark root}"
AB_OUT="${AB_OUT:-}"
if [ "$AB_LOCAL" != "1" ] && [ -z "$AB_SSH_HOST" ]; then
  echo "Set AB_SSH_HOST for remote execution, or AB_LOCAL=1 for local execution" >&2
  exit 2
fi
if [ "$AB_LOCAL" = "1" ] && [ -z "${AB_DEVS:-}" ]; then
  AB_DEVS="0 1"
fi

if [ "$AB_LOCAL" = "1" ]; then
  echo "AB_LOCAL=1  CANN_BENCH_HOME=${CANN_BENCH_HOME:?missing benchmark root}  卡 ${AB_DEVS:-0 1}"
  # KTP Job 独占 remap 后的 0,1，demo1 上别人的 kernel_eval 不在这个 PID 命名空间。
  LEFT=$(ps -eo command 2>/dev/null | grep -c "[k]ernel_eval" || true)
  LEFT=$(echo "${LEFT:-0}" | tr -d " \r")
  if [ "${LEFT:-0}" != "0" ]; then
    echo "⚠ 本容器内已有 ${LEFT} 个 kernel_eval（同 Job 重入？）" >&2
    ps -eo etime,command 2>/dev/null | grep "[k]ernel_eval" | cut -c1-110 >&2 || true
    [ "${AB_FORCE:-0}" = "1" ] || exit 3
  fi
else
  LEFT=$("${DEV[@]}" 'ps -eo command | grep -c "[k]ernel_eval"' 2>/dev/null | tr -d " \r")
  if [ "${LEFT:-0}" != "0" ]; then
    echo "⛔ 远端还有 ${LEFT} 个 kernel_eval 进程在跑，本次测量会撞卡。" >&2
    "${DEV[@]}" 'ps -eo etime,command | grep "[k]ernel_eval" | cut -c1-110' 2>/dev/null >&2
    echo "   要清干净：ssh … 'pkill -9 -f kernel_eval; sleep 3; pkill -9 -f kernel_eval'" >&2
    echo "   不要 AB_FORCE=1 硬抢；KTP 一算子一 Job npu:2 才是并行。" >&2
    [ "${AB_FORCE:-0}" = "1" ] || exit 3
  fi
fi

STAGE="$(mktemp -d)"
ARMS=()
for spec in "$@"; do
  name="${spec%%:*}"; src="${spec#*:}"
  [ -d "$src/op_kernel" ] || { echo "⛔ ${name}: ${src} 下没有 op_kernel/" >&2; exit 2; }
  pkg="$STAGE/arm_${name}"
  cp -r "$TEMPLATE" "$pkg" && rm -rf "$pkg"/csrc/ops/*/ 2>/dev/null
  # 模板 setup.py 用 os.cpu_count() 得到 640，然后 `cmake --build --parallel 640`。
  # 4 路并发就是 2560 个 make 任务抢 640 核 —— 这才是"8 路静默丢计时"的真因，
  # 不是卡不够（16 个 chip 一直是空的）。这里给它一个环境变量旋钮。
  # ⚠ 只改**本地测量**用的这份拷贝；push910.py 提交时用的是另一份原始模板。
  perl -pi -e "s/cpu_count = os\.cpu_count\(\) or 2/cpu_count = int(os.environ.get(chr(67)+chr(65)+chr(78)+chr(78)+chr(95)+chr(66)+chr(85)+chr(73)+chr(76)+chr(68)+chr(95)+chr(74)+chr(79)+chr(66)+chr(83)) or (os.cpu_count() or 2))/" "$pkg/setup.py"
  cp -r "$src" "$pkg/csrc/ops/"
  # 模板 cann_bench/__init__.py 写死 add/sqrt。runner 扫 Python 接口对不上
  # 就算子 ⇒ 回落成 53 算子。真值源是 plugin m.def schema。
  python3 "$HERE/gen_init.py" "$pkg"
  ARMS+=("$name")
done
find "$STAGE" -name "._*" -delete 2>/dev/null
( cd "$STAGE" && tar czf arms.tgz $(ls -d arm_*) )
LSZ=$(bytes_of "$STAGE/arms.tgz")
ARMS_B64_FILE="$STAGE/arms.b64"
base64 < "$STAGE/arms.tgz" > "$ARMS_B64_FILE"

echo "算子 ${OP}   臂 ${ARMS[*]}   每臂 ${REP} 次   并发 ${AB_CONC:-4}  起始卡 ${AB_DEV0:-0}"

{
  printf 'OP=%q\nREP=%q\nARMS=%q\nAB_CONC=%q\nAB_DEV0=%q\nAB_TAG=%q\nAB_CANN=%q\nAB_DEVS=%q\nLSZ=%q\nCANN_BUILD_JOBS=%q\nCANN_BENCH_HOME=%q\nAB_OUT=%q\n' \
         "$OP" "$REP" "${ARMS[*]}" "${AB_CONC:-4}" "${AB_DEV0:-0}" "${AB_TAG:-0}" "${AB_CANN:-}" \
         "${AB_DEVS:-}" "$LSZ" "${CANN_BUILD_JOBS:-16}" \
         "${CANN_BENCH_HOME}" "${AB_OUT:-}"
  printf 'base64 -d > /tmp/ab_arms_${AB_TAG}.tgz <<'\''__ARMS_B64__'\''\n'
  cat "$ARMS_B64_FILE"
  printf '\n__ARMS_B64__\n'
  cat <<'REMOTE'
set -u
export CANN_BUILD_JOBS=${CANN_BUILD_JOBS:-16}
RSZ=$(stat -c %s /tmp/ab_arms_${AB_TAG}.tgz 2>/dev/null || echo MISSING)
if [ "$RSZ" != "$LSZ" ]; then
  echo "⛔ arms 包对账失败：本地 ${LSZ}B vs 远端 ${RSZ}B（exec 流内嵌解码后）" >&2
  exit 4
fi
echo "arms 包对账 ✓ ${RSZ}B"
CANN_BENCH_HOME=${CANN_BENCH_HOME:?missing benchmark root}
cd "$CANN_BENCH_HOME" || { echo "⛔ CANN_BENCH_HOME 不存在: $CANN_BENCH_HOME" >&2; exit 4; }
AB=/tmp/ab_${AB_TAG}
rm -rf $AB && mkdir -p $AB && tar xzf /tmp/ab_arms_${AB_TAG}.tgz -C $AB
find $AB -name "._*" -delete 2>/dev/null

# ── AB_CANN=9 的两层坑（自愈式私有 toolkit 根）────────────────────────────
# 需要 CANN 9 的算子：weight_quant_batch_matmul（precision_fallback.h:27 include
# adv_api/matmul/matmul_tiling.h，该头只在 cann-9.0.0）、grouped_matmul / nms / gru
# （AscendC::Compares）、unsorted_segment_sum（DisableDmaAtomic）。
# 🔴 但**直接把 ASCEND_HOME_PATH 指向 cann-9.0.0 会让整轮报"无报告"**，两层原因：
#   1. cann-9.0.0 缺 runtime/version.info ⇒ torch_npu 的 _get_cann_version 读未初始化
#      缓冲 ⇒ UnicodeDecodeError 0xf4 ⇒ import torch 崩 ⇒ harness 宣布
#      `cann_bench_utils 未安装` ⇒ 自动重建失败 ⇒ **所有臂"无报告"（不是 0/20，
#      看起来像机器脏）**。三个 agent 各自踩过这个。
#   2. 修好 1 之后还会 `_C.abi3.so: undefined symbol: aclrtLaunchKernelWithHostArgs`
#      —— 该符号只在 cann-9.0.0/lib64，必须前置到 LD_LIBRARY_PATH。
# 修法来自 arms/wqbmm_v2/ab910_wq.sh（7/7 次 `计时 20/20 过 20/20`，一次就通）。
# ⚠ 不要从 arms/gmm_cube_mc/ab910_gmm.sh 上游化 —— 那份把下面"钉死
#   cann_final_eval*.json"的修法退回成了 `*eval*.json` glob。
if [ -n "${AB_CANN:-}" ]; then
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
      "$C9/runtime/version.info" \
      /usr/local/Ascend/latest/runtime/version.info; do
      if [ -f "$c" ] && grep -q 'required_package_opp_version' "$c"; then VI=$c; break; fi
    done
    if [ -n "$VI" ]; then
      cp "$VI" /tmp/cann9_ab/runtime/version.info
    else
      # 容器里无任何真 version.info：内嵌登录节点实测的 8.3.0.1.200 全文兜底。
      cat > /tmp/cann9_ab/runtime/version.info <<'VINFO'
Version=8.3.0.1.200
version_dir=8.3.RC1
timestamp=20251027_211954946
runtime_acl_version=1.0
runtime_dvpp_version=1.0
required_driver_ascendhal_version=4.0.0
required_driver_dvppkernels_version=1.1
required_driver_tsfw_version=1.0
required_opp_abi_version=">=6.3, <=8.3"
required_package_amct_acl_version="8.3"
required_package_aoe_version="8.3"
required_package_compiler_version="8.3"
required_package_fwkplugin_version="8.3"
required_package_hccl_version="8.3"
required_package_nca_version="8.3"
required_package_ncs_version="8.3"
required_package_opp_version="8.3"
required_package_opp_kernel_version=">=7.6, <=8.3"
required_package_toolkit_version="8.3"
VINFO
      echo "ℹ 容器内无 version.info 源，内嵌 8.3.0.1.200 全文兜底"
    fi
  fi
  cat > /tmp/cann9_ab_run.sh <<'WRAP'
#!/bin/bash
export ASCEND_HOME_PATH=/tmp/cann9_ab
export ASCEND_OPP_PATH=/tmp/cann9_ab/opp
export LD_LIBRARY_PATH=/usr/local/Ascend/cann-9.0.0/lib64:$LD_LIBRARY_PATH
exec "$@"
WRAP
  chmod +x /tmp/cann9_ab_run.sh
fi

# 生成 (臂, 重复序号) 任务表
AB_CONC=${AB_CONC:-4}; AB_DEV0=${AB_DEV0:-0}
# ── AB_DEVS：显式卡清单（例 AB_DEVS="0 1 2 3 6 7 10 11 12 13 14 15"）───────────
# 这台机有 **16 个逻辑 device**（8 个 Ascend910 模块 × 2 chip，`npu-smi info` 里
# 16 条 bus-id）。原来的 `AB_DEV0 + slot % AB_CONC` 只能取**连续区间**，
# 而实际情况总是「16 张卡里有几张被别人占着」⇒ 想吃满剩下的空卡就必须能跳过中间那几张。
# 给了 AB_DEVS 就用它，并把 AB_CONC 覆盖成清单长度（波次大小 = 卡数）。
DEVLIST=()
if [ -n "${AB_DEVS:-}" ]; then
  for x in $AB_DEVS; do DEVLIST+=("$x"); done
  AB_CONC=${#DEVLIST[@]}
else
  for i in $(seq 0 $((AB_CONC - 1))); do DEVLIST+=("$((AB_DEV0 + i))"); done
fi
echo "卡清单: ${DEVLIST[*]}   波次大小 ${AB_CONC}"
JOBS=""
for a in $ARMS; do
  for r in $(seq 1 $REP); do JOBS="$JOBS $a:$r"; done
done

slot=0
for job in $JOBS; do
  a="${job%%:*}"; r="${job##*:}"
  d=${DEVLIST[$((slot % AB_CONC))]}
  work=$AB/run_${a}_${r}
  rm -rf "$work" && cp -r $AB/arm_${a} "$work"
  (
    if [ ! -x /tmp/venv_${d}/bin/python ]; then
      echo "venv /tmp/venv_${d} 不存在，python3 -m venv --system-site-packages"
      python3 -m venv --system-site-packages /tmp/venv_${d} || {
        echo "⛔ 无法创建 /tmp/venv_${d}" >&2
        exit 5
      }
    fi
    /tmp/venv_${d}/bin/pip uninstall -y cann-bench cann_bench >/dev/null 2>&1
    if [ -n "${CANN_BENCH_UTILS_BUILD_DIR:-}" ]; then
      PRIVATE_BENCH="${CANN_BENCH_UTILS_BUILD_DIR}/dev${d}"
    else
      PRIVATE_BENCH=/tmp/venv_${d}/cann_bench_private
    fi
    # src 必须真拷贝（cmake 写 build/）。scripts 必须真拷贝：run_evaluation.sh
    # 用 BASH_SOURCE 推 PROJECT_ROOT，symlink 会 pwd 回到 share 树，cmake 又抢 overlay。
    # tasks/configs 只读 —— symlink。禁止 `cp tasks || true` 静默缺目录
    # （fix-v1 INFRAFAIL: tasks 目录不存在: /tmp/venv_0/cann_bench_private/tasks）。
    mkdir -p "$PRIVATE_BENCH"
    if [ ! -d "$PRIVATE_BENCH/src/cann_bench_utils" ]; then
      echo "  创建私有 cann_bench src+scripts -> $PRIVATE_BENCH"
      cp -a "$CANN_BENCH_HOME/src" "$PRIVATE_BENCH/"
      cp -a "$CANN_BENCH_HOME/scripts" "$PRIVATE_BENCH/"
    fi
    if [ ! -e "$PRIVATE_BENCH/tasks" ]; then
      if [ -d "$CANN_BENCH_HOME/tasks" ]; then
        ln -sfn "$CANN_BENCH_HOME/tasks" "$PRIVATE_BENCH/tasks" \
          || cp -a "$CANN_BENCH_HOME/tasks" "$PRIVATE_BENCH/tasks"
      fi
    fi
    if [ ! -d "$PRIVATE_BENCH/tasks" ]; then
      echo "⛔ tasks 目录不存在: $CANN_BENCH_HOME/tasks -> $PRIVATE_BENCH/tasks" >&2
      exit 5
    fi
    for LINKDIR in configs data bench_lab; do
      if [ -e "$CANN_BENCH_HOME/$LINKDIR" ] && [ ! -e "$PRIVATE_BENCH/$LINKDIR" ]; then
        ln -sfn "$CANN_BENCH_HOME/$LINKDIR" "$PRIVATE_BENCH/$LINKDIR" || true
      fi
    done
    # AB_CANN=9 时把 toolkit 换成 cann-9.0.0。开发机默认 ASCEND_HOME_PATH 指向
    # ascend-toolkit/latest = 8.3.RC1，而**计分平台用的是 9.x**——现役 dilation_2d /
    # grouped_matmul / nms / gru 用了 AscendC::Compares，unsorted_segment_sum 用了
    # DisableDmaAtomic，这些符号 8.3.RC1 里根本不存在 ⇒ 在开发机上 cann_compile_failed，
    # 连基线臂都 0/20。两台机上其实都装着 /usr/local/Ascend/cann-9.0.0。
    # torch 2.10 + torch_npu：父进程 import 时 _autoload 会把 AUTOLOAD 写回 1，
    # eval-child 继承后再 import torch 自动加载 npu，接着 import torch_npu 炸成
    # "Two accelerators: npu and npu"。必须在 run_evaluation 前钉死 0。
    export TORCH_DEVICE_BACKEND_AUTOLOAD=0
    PIP_FORCE_REINSTALL=1 PATH=/tmp/venv_${d}/bin:$PATH \
      TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
      CANN_BUILD_JOBS=${CANN_BUILD_JOBS:-16} \
      CANN_BENCH_HOME="$PRIVATE_BENCH" \
      ${AB_CANN:+bash /tmp/cann9_ab_run.sh} \
      "$PRIVATE_BENCH/scripts/run_evaluation.sh" \
      --source-dir "$work" --operator "$OP" --device npu --device-id $d \
      --reports-dir $AB/rep_${a}_${r} >"$work/eval.log" 2>&1
    # 🔴 容器 /tmp 是易失的：若 cann_bench_utils 编译失败，把**容器内**真实 build.log
    #    + 关键 env 落到持久化 $AB_OUT（否则只剩登录节点上 5 天前的陈旧副本，误诊）。
    if grep -q "cann_bench_utils 编译失败\|无 final 报告" "$work/eval.log" 2>/dev/null; then
      DIAG=${AB_OUT:-$AB}/diag_${a}_${r}_dev${d}
      mkdir -p "$DIAG"
      cp /tmp/cann_bench_utils_build.log "$DIAG/build.log" 2>/dev/null || echo "no build.log" > "$DIAG/build.log"
      { echo "ASCEND_HOME_PATH=${ASCEND_HOME_PATH:-<unset>}";
        echo "ASCEND_OPP_PATH=${ASCEND_OPP_PATH:-<unset>}";
        echo "LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-<unset>}";
        echo "--- /tmp/cann9_ab/runtime/version.info ---"; cat /tmp/cann9_ab/runtime/version.info 2>&1;
        echo "--- ls cann-9.0.0/opp ---"; ls -ld /usr/local/Ascend/cann-9.0.0/opp 2>&1;
        echo "--- ls ascend-toolkit/latest/opp ---"; ls -ld /usr/local/Ascend/ascend-toolkit/latest/opp 2>&1;
        echo "--- python import torch_npu under wrapper ---";
        bash /tmp/cann9_ab_run.sh /tmp/venv_${d}/bin/python -c "import torch, torch_npu; print('torch_npu OK', torch_npu.__version__)" 2>&1 | tail -8;
      } > "$DIAG/env.txt" 2>&1
    fi
    python3 - "$a" "$r" "$d" "$AB" "$work/eval.log" <<'PY'
import json, glob, os, sys
arm, rep, dev, ab = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
elog = sys.argv[5] if len(sys.argv) > 5 else ""
# 🔴 每个 rep 目录里有**三份**阶段报告：cann_correctness_eval_* / cann_performance_eval_*
#    / cann_final_eval_*。只有 final 那份带 elapsed_us。按 mtime 取最后一份不保险
#    （scatter_hidfix 就因为读到 correctness 那份，把 3 次干净的 20/20 误判成
#    「计时缺失,本次作废」）。这里显式钉死 final。
fs = glob.glob(f"{ab}/rep_{arm}_{rep}/cann_final_eval*.json")
if not fs:                                   # 没有 final = 这一跑真的没到 perf 阶段
    other = glob.glob(f"{ab}/rep_{arm}_{rep}/*eval*.json")
    stage = ",".join(sorted(os.path.basename(x).split("_eval")[0] for x in other)) or "无"
    print(f"  {arm:<10} #{rep} dev{dev}  ⚠ 无 final 报告（只到 {stage} 阶段）")
    if elog and os.path.isfile(elog):
        try:
            tail = open(elog, errors="replace").read()[-1800:]
        except OSError:
            tail = ""
        if tail.strip():
            print("    --- eval.log ---")
            print("\n".join("    " + x for x in tail.strip().splitlines()[-40:]))
    raise SystemExit
d = json.load(open(sorted(fs, key=os.path.getmtime)[-1]))
for o in (d.get("operators") or []):
    cs = o.get("cases") or []
    t = sum(1 for x in cs if isinstance(x.get("elapsed_us"), (int, float)) and x["elapsed_us"] > 0)
    ok = "" if (cs and t == len(cs)) else "  ⚠计时缺失,本次作废"
    # 🔴 passed_cases 必须打出来：只看"计时 N/20"会把 correctness_failed 误当抢卡，
    #    softmax_hla 因此白跑一整轮（13/20 全是算错，不是丢计时）。
    tot = o.get("total_cases") or len(cs)
    ps = o.get("passed_cases")
    if ps is None:
        ps = sum(1 for x in cs if (x.get("accuracy") or {}).get("passed") is True)
    bad = "  🔴正确性退化" if (tot and ps < tot) else ""
    print(f"  {arm:<10} #{rep} dev{dev}  计时 {t}/{len(cs)}  过 {ps}/{tot}  "
          f"分={o.get('score') or 0:.2f}  fn={o.get('function_score') or 0:.1f}  "
          f"sp={o.get('avg_speedup') or 0:.2f}{ok}{bad}")
PY
  ) &
  slot=$((slot + 1))
  if [ $((slot % AB_CONC)) -eq 0 ]; then wait; fi
done
wait
if [ -n "${AB_OUT:-}" ]; then
  mkdir -p "$AB_OUT"
  cp -a $AB/rep_* "$AB_OUT"/ 2>/dev/null || true
  echo "AB_OUT=$AB_OUT"
fi
REMOTE
} > "$STAGE/script.sh"

if [ "${AB_LOCAL:-0}" = "1" ]; then
  bash "$STAGE/script.sh"
  rc=$?
  rm -rf "$STAGE"
  exit $rc
fi

if [ "${AB_DETACH:-0}" = "1" ]; then
  SSZ=$(bytes_of "$STAGE/script.sh")
  PUSHED=0
  if [ -n "${AB_SCP_USE_CONFIG:-}" ]; then
    SCP_PUSH=(scp -O -o BatchMode=yes \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ServerAliveInterval=5 -o ServerAliveCountMax=4 -o ConnectTimeout=20 -P "${AB_SSH_PORT}")
  else
    SCP_PUSH=(scp -F /dev/null -O "${SSH_IFACE_OPTS[@]}" -o BatchMode=yes \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ServerAliveInterval=5 -o ServerAliveCountMax=4 -o ConnectTimeout=20 -P "${AB_SSH_PORT}")
  fi
  for _try in 1 2 3; do
    if [[ "$XFER_MODE" == 1 ]]; then
      env MSYS2_ARG_CONV_EXCL='*' python "$XFER" put "$(cygpath -w "$STAGE/script.sh")" "remote=/tmp/ab_script_${AB_TAG:-0}.sh" >/dev/null 2>&1
    else
      "${SCP_PUSH[@]}" "$STAGE/script.sh" "${AB_SSH}:/tmp/ab_script_${AB_TAG:-0}.sh" >/dev/null 2>&1
    fi
    RSZ2=$("${DEV[@]}" "stat -c %s /tmp/ab_script_${AB_TAG:-0}.sh 2>/dev/null" 2>/dev/null | tr -d " \r")
    [ "${RSZ2:-0}" = "$SSZ" ] && { PUSHED=1; break; }
    echo "  脚本推送第 ${_try} 次未成（本地 ${SSZ}B vs 远端 ${RSZ2:-0}B），$((_try*10))s 后重试…" >&2
    sleep $((_try * 10))
  done
  [ "$PUSHED" = "1" ] || { echo "⛔ 脚本推送对账失败 3 次：本地 ${SSZ}B vs 远端 ${RSZ2:-0}B" >&2; exit 4; }
  "${DEV[@]}" "setsid nohup bash /tmp/ab_script_${AB_TAG:-0}.sh > /tmp/ab_out_${AB_TAG:-0}.log 2>&1 < /dev/null & echo AB_DETACHED_PID=\$!; sleep 8" 2>/dev/null
  echo "已脱管：远端日志 /tmp/ab_out_${AB_TAG:-0}.log（本地 ssh 掉线不影响波次）"
else
  if [[ "$XFER_MODE" == 1 ]]; then
    python "$XFER" exec-stdin 7200 bash -s < "$STAGE/script.sh"
  else
    cat "$STAGE/script.sh" | "${DEV[@]}" "bash -s"
  fi
fi
rm -rf "$STAGE"
