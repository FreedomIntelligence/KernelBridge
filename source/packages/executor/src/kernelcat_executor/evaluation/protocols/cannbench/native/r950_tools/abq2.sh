#!/bin/bash
# abq2.sh —— 第二条 A/B 队列，跑在 **4-7 号卡** 上，与 abq.sh（0-3 号卡）并行。
#
# 为什么可以并行（推翻了 abq.sh 顶上"必须串行"的注释）：
# 那条注释的根因写对了一半——静默丢计时的真因是 `cmake --build --parallel 640`
# 叠加并发（4 路 × 640 = 2560 个 make 任务抢 640 核），不是卡不够。
# 压到 AB_JOBS=80 之后，两条队列各 4 路 = 8 个 build × 80 = 640，正好打满不超卖。
# 机器有 16 个 chip，0-3 和 4-7 完全不重叠，venv_4..7 也是独立的。
#
# ⚠ AB_FORCE=1：ab910.sh 的前置检查会因为另一条队列的 kernel_eval 进程而拒跑。
# 那个检查是为了挡"僵尸进程占卡"，这里是**故意**的并发，所以显式绕过。
# 代价：如果真有僵尸残留，这条队列不会替你发现——所以只在人盯着的时候开。
S=/private/tmp/claude-501/-Users-jerry-Desktop-kernelagent/e8f02d5f-63d8-4d9a-9fbc-aae2af0b07c4/scratchpad
cd "$(dirname "$0")"
Q=${QFILE:-abq2.tsv}
D=${DONEFILE:-.abq_done2}
DEV0=${DEV0:-4}
touch "$D"
while true; do
  progressed=0
  while IFS=$'\t' read -r arm op live shim; do
    [ -z "$arm" ] && continue
    case "$arm" in \#*) continue;; esac
    [ -f "$arm/NOTES.md" ] || continue
    grep -qxF "$arm" "$D" && continue
    tag="r$(echo "$op" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9' | cut -c1-10)"
    CANNFLAG=""; [ "$shim" = "cann9" ] && CANNFLAG=9
    echo "=== $(date +%T) $op  arm=$arm ===" >> "${RESFILE:-ABQ_RESULTS_B.txt}"
    AB_FORCE=1 AB_TAG="$tag" AB_CONC=8 AB_DEV0="$DEV0" AB_JOBS=80 AB_CANN="$CANNFLAG" \
      ./ab910.sh "$op" 2 live:"$live" arm:"$arm" >> "${RESFILE:-ABQ_RESULTS_B.txt}" 2>&1
    echo "$arm" >> "$D"
    progressed=1
  done < "$Q"
  [ $progressed -eq 0 ] && sleep 45
done
