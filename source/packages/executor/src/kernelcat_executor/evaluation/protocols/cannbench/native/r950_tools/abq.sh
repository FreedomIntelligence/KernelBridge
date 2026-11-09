#!/bin/bash
# abq.sh —— A/B 队列驱动器（0-3 号卡）。abq2.sh 是它的 4-7 号卡孪生。
#
# 队列文件 abq.tsv 每行： <arm目录> <TAB> <OpCamel> <TAB> <live目录> [<TAB>shim|cann9]
# 只跑 NOTES.md 已存在（= 作者写完了）且还没测过的行。
#
# ⚠ 两条队列必须写**不同的**结果文件：它们交错追加到同一个文件时，
# autopush 的解析器会把 A 的读数挂到 B 的表头下，导致提交错的臂。
# ⚠ AB_FORCE=1：ab910.sh 的前置检查会因为另一条队列的 kernel_eval 进程而拒跑。
# 那个检查是防"僵尸进程占卡"的，这里是故意的并发。2026-08-02 少了这一行，
# 队列 A 在 100 秒里把 5 个算子全部秒退**并记进 .abq_done**，等于白白丢掉。
cd "$(dirname "$0")"
RES="${RESFILE:-ABQ_RESULTS_A.txt}"
Q="${QFILE:-abq.tsv}"
D="${DONEFILE:-.abq_done}"
touch "$D"
while true; do
  progressed=0
  while IFS=$'\t' read -r arm op live shim; do
    [ -z "$arm" ] && continue
    case "$arm" in \#*) continue;; esac
    [ -f "$arm/NOTES.md" ] || continue
    grep -qxF "$arm" "$D" && continue
    A="$arm"; L="$live"
    if [ "$shim" = "shim" ]; then
      rm -rf /tmp/q_a /tmp/q_l; cp -r "$arm" /tmp/q_a; cp -r "$live" /tmp/q_l
      ./localshim.sh /tmp/q_a >/dev/null; ./localshim.sh /tmp/q_l >/dev/null
      A=/tmp/q_a; L=/tmp/q_l
    fi
    tag="q$(echo "$op" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9' | cut -c1-10)"
    CANNFLAG=""; [ "$shim" = "cann9" ] && CANNFLAG=9
    echo "=== $(date +%T) $op  arm=$arm ===" >> "$RES"
    AB_FORCE=1 AB_TAG="$tag" AB_CONC=8 AB_DEV0="${DEV0:-0}" AB_JOBS=80 AB_CANN="$CANNFLAG" \
      ./ab910.sh "$op" 2 live:"$L" arm:"$A" >> "$RES" 2>&1
    echo "$arm" >> "$D"
    progressed=1
  done < "$Q"
  [ $progressed -eq 0 ] && sleep 45
done
