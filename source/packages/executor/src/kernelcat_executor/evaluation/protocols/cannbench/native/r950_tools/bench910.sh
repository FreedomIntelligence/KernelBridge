#!/usr/bin/env bash
# 在开发机的**真 910c NPU** 上跑官方评测，拿到真分数与真加速比。零 CANN credit。
#
#   ./bench910.sh <算子CamelName> <源码目录> [标签]
#   例: ./bench910.sh ForeachNorm ../cann_submit/candidates/foreach_norm base
#
# 为什么这条闭环成立（2026-08-02 标定过）：
#   开发机 harness 编在 ascend-toolkit/latest = CANN 8.3.RC1 = dav-c220，
#   而开发机那块 NPU 就是 910c = dav-c220。**三者匹配**，所以能复现榜面。
#   实测标定：ResizeBilinear 本地 57.03 / 0.29x  vs 榜上 56.1 / 0.23x（差约 1 分）。
#
# ⚠ 适用边界：
#   * 只对 **910c** 有效。950pr(dav-c310) 的分不能用这里的结果外推
#     —— FINDINGS-019：同源码可以 c220 20/20 而 c310 1/20。
#   * 源码若 include 了 `adv_api/...`（CANN 9.0.0 才有的布局，如 grouped_matmul
#     的 matmul_tiling.h），在 8.3 的 harness 下编不过。这类算子走不了这条路。
#   * 这台机的 sshd 对任何命令都返回 255，退出码不可信；本脚本靠输出内容判断。

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV=(ssh -o UpdateHostKeys=no -o StrictHostKeyChecking=no -p 31222 demo1@10.1.30.201)
TEMPLATE="$HERE/../cann_submit/template"

# 这台机是**共用**的：2026-08-02 12:5x 起 0-8 号卡被别人占到 AICore 100%、HBM 64/65 GB，
# 我们的 job 直接 NPU OOM。跑之前先 `npu-smi info` 挑一张空卡，用 B910_DEV 指定。
# ⚠ 邻卡满载时的计时不可比：换卡后**两条臂都要重测**，不能拿旧读数当对照。
DEVID="${B910_DEV:-0}"

OP="${1:?用法: bench910.sh <算子CamelName> <源码目录> [标签]}"
SRC="${2:?缺源码目录}"
TAG="${3:-$(basename "$SRC")}"
[ -d "$SRC/op_kernel" ] || { echo "⛔ $SRC 下没有 op_kernel/" >&2; exit 2; }

PKG="/tmp/b910_${TAG}"
rm -rf "$PKG" && cp -r "$TEMPLATE" "$PKG" && rm -rf "$PKG"/csrc/ops/*/ 2>/dev/null
cp -r "$SRC" "$PKG/csrc/ops/"
find "$PKG" -name "._*" -delete 2>/dev/null
tar czf "${PKG}.tgz" -C /tmp "$(basename "$PKG")"
# scp 不要加 -q：这台机上 -q 会返回非零
scp -o UpdateHostKeys=no -o StrictHostKeyChecking=no -P 31222 "${PKG}.tgz" \
    demo1@10.1.30.201:/tmp/ >/dev/null 2>&1

"${DEV[@]}" "bash -s" <<REMOTE
B=\$(basename "$PKG")
rm -rf /tmp/\$B && tar xzf /tmp/\$B.tgz -C /tmp && find /tmp/\$B -name "._*" -delete 2>/dev/null
cd /home/demo1/cann-bench-20260722
echo "──── $OP [$TAG] dev=$DEVID \$(date '+%H:%M:%S') ────"
./scripts/run_evaluation.sh --source-dir /tmp/\$B --operator $OP --device npu --device-id $DEVID 2>&1 \
  | grep -E "通过用例数|失败用例数|通过率|综合得分|平均加速比|编译失败|\[ERROR\]" | head -8
if grep -qiE "fatal error|error:" /tmp/\$B/_compile.log 2>/dev/null; then
  echo "  编译错误:"; grep -iE "fatal error|error:" /tmp/\$B/_compile.log | head -3 | sed 's/^/    /' | cut -c1-150
fi
REMOTE
