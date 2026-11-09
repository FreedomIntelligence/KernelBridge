#!/usr/bin/env bash
# 打印当前**没有 eval-child 占用**且 HBM 空闲的一对相邻 device id（供 AB_DEV0 用）。
# 这台机是共用的：2026-08-03 已经和另一条臂（Gelu 阴性对照）在 dev6 上撞过一次，
# 三个 eval-child 挤同一张卡 => 计时被拉高，读数作废。**每次跑之前重新挑。**
SSH=(ssh -o UpdateHostKeys=no -o StrictHostKeyChecking=no -o ConnectTimeout=25 -p 31222 demo1@10.1.30.201)
"${SSH[@]}" 'busy=$(ps -eo command | grep "[e]val-child" | grep -o "device-id [0-9]*" | awk "{print \$2}" | sort -u | tr "\n" " ")
hbm=$(npu-smi info | grep -oE "[0-9]+/ 65536" | awk -F"/" "{print \$1}")
i=0; for h in $hbm; do if [ "$h" -gt 10000 ]; then busy="$busy $i"; fi; i=$((i+1)); done
echo "busy: $busy"
for d in 0 2 4 6 8 10 12 14; do
  ok=1
  for b in $busy; do [ "$b" = "$d" ] && ok=0; [ "$b" = "$((d+1))" ] && ok=0; done
  # 只有 /tmp/venv_0..7 存在
  [ $d -gt 6 ] && ok=0
  [ $ok = 1 ] && { echo "FREE_PAIR=$d"; exit 0; }
done
echo "FREE_PAIR=none"'
