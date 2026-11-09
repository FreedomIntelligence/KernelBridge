#!/bin/bash
# localshim.sh <dir>  —— 把 CANN 9.x 才有的 API 改名成开发机 8.3.RC1 的等价物。
# ⚠ 只用于**本地测量**。提交必须用原始源码（计分平台上 Compares/NumericLimits 是存在的）。
# 两个臂都跑同一个补丁 ⇒ A/B 差值仍然有效。
# 等价关系：
#   AscendC::Compares(dst,src,scalar,mode,n)          == AscendC::CompareScalar(同签名)
#   AscendC::NumericLimits<float>::NegativeInfinity() == -__builtin_inff()
#   AscendC::NumericLimits<float>::NegativeInfinity(t,n) == AscendC::Duplicate(t, -__builtin_inff(), n)
#   AscendC::NumericLimits<float>::QuietNaN()         == __builtin_nanf("")
set -e
d="$1"
for f in $(grep -rl "AscendC::Compares\|AscendC::NumericLimits" "$d" 2>/dev/null); do
  perl -0pi -e '
    s/AscendC::NumericLimits<float>::NegativeInfinity\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*([^)]+)\)/AscendC::Duplicate($1, -__builtin_inff(), $2)/g;
    s/AscendC::NumericLimits<float>::NegativeInfinity\(\)/(-__builtin_inff())/g;
    s/AscendC::NumericLimits<float>::QuietNaN\(\)/(__builtin_nanf(""))/g;
    s/AscendC::Compares\(/AscendC::CompareScalar(/g;
  ' "$f"
  echo "  shim: $f"
done
