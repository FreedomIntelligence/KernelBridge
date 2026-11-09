#!/usr/bin/env bash
# 910c 真机 A/B —— **串行**。零 CANN credit。
#
#   ./abserial.sh <算子CamelName> <重复次数> <臂名>:<源码目录> [<臂名>:<源码目录> ...]
#
# ── 为什么不用 ab910.sh 的 4 路并发（2026-08-02 定案）──────────────────────
# 那 4 个 /tmp/venv_N 是 `python -m venv --system-site-packages` 建的，而
# cann_bench 装在**系统** site-packages：
#     /tmp/venv_0/bin/python -c "import cann_bench" -> /usr/local/python3.11.15/.../cann_bench
# ⇒ venv 对 cann_bench **零隔离**，4 个槽共用同一份安装。并发跑不同臂时，
#   谁最后装完谁说了算，四个槽读的是同一个二进制。
# 两种表现：
#   (a) 全 0/20「无法找到算子 X」——看得出来；
#   (b) **安静地给出漂亮但相同的分数**——看不出来，且会把任何真实改动读成 "无提升"。
# 之前那次「4 路 A/A 散布 0.38，判定安全」不构成反例：A/A 两臂本来就同源，
# 共用一份安装也测不出差别。
#
# 串行则没有这个问题：harness 每次 uninstall+install 完才跑，下一次才开始。
# ForeachNorm 的串行 A/B（68.15/68.42 vs 71.44/71.37）已被榜面确认
# （job_f9199279daff 得 70.77，对旧行 67.42 = +3.35，本地预测 +3.12）。
#
# 代价：约 3 分钟/次。臂数 × 重复数 直接乘。
#
# 判定门槛：噪声底 sigma ~ 0.48 分 ⇒ 效应 < 1.0 分按 null 处理。

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OP="${1:?用法: abserial.sh <算子> <重复次数> <臂名>:<源码目录>...}"; shift
REP="${1:?缺重复次数}"; shift
[ $# -ge 1 ] || { echo "至少给一个臂" >&2; exit 2; }

for spec in "$@"; do
  src="${spec#*:}"
  [ -d "$src/op_kernel" ] || { echo "⛔ ${spec%%:*}: ${src} 下没有 op_kernel/" >&2; exit 2; }
done

echo "═══ ${OP}  串行 A/B  每臂 ${REP} 次 ═══"
for r in $(seq 1 "$REP"); do
  echo "── 第 ${r} 轮 ──"
  for spec in "$@"; do
    name="${spec%%:*}"; src="${spec#*:}"
    out=$("$HERE/bench910.sh" "$OP" "$src" "ab_${name}" 2>&1)
    line=$(echo "$out" | grep -E "通过用例数|综合得分|平均加速比" | tr -d '\n')
    if [ -z "$line" ]; then
      line=$(echo "$out" | grep -E "\[ERROR\]|编译|error" | head -2 | tr -d '\n')
      printf '  %-8s ⚠ %s\n' "$name" "${line:-无输出}"
    else
      printf '  %-8s %s\n' "$name" "$line"
    fi
  done
done
