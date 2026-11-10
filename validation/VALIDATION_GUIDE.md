# 安装与功能验证

在源码根目录执行以下命令。Node、Python 与依赖由 install.py 或 restore.sh 配置。

```sh
python3 -I -B install.py --verify-only
npm run check:types --prefix source/apps/agent
node --test validation/*.mjs
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s validation -p 'test_*.py' -v
```

Python 测试环境需要锁定运行时依赖；正确性相关测试使用 Torch。NPU 测试通过任务 profile 选择已有 Torch/torch_npu 和 CANN 的解释器。

## 验证范围

| 类别 | 检查内容 |
| --- | --- |
| Pi 基础工具 | 文件读取、修改、shell、工具参数与上下文 |
| 权限与隔离 | 只读参考、可写范围、库外符号链接、受控环境 |
| 正确性 | 独立输入、参考输出保护、非法输入修改、shape/dtype 与精度 |
| 执行恢复 | 工具错误、超时、未决效果、原身份对账 |
| 源码与验收 | 调用前冻结、哈希匹配、已验收候选选择 |
| 会话归档 | 原生 JSONL/checkpoint、文件哈希、session ID、leaf 与上下文 |
| 跨环境 | 虚拟环境、CANN 路径、带空格目录、模式与源码续接 |

## Goal 协议测试

以下测试使用真实 Pi SDK 和 LoopX，模型与设备响应使用合成夹具。

```sh
python3 validation/goal-workflow.py \
  --node /absolute/node --output /absolute/checks/goal-complete
python3 validation/goal-workflow.py \
  --node /absolute/node --output /absolute/checks/goal-multi --multi-trial
```

它们分别验证独立验收完成 Goal，以及两个连续 Trial 的父候选、预算和调用上限。输出目录保存 SDK 事件、LoopX 状态与 assertions.json。

## 设备与恢复验证

提交 Vector/Cube 示例，检查 baseline_check、operator_check、operator_evaluate 和 Goal acceptance。使用 export.mjs 导出后，通过 restore.sh 在新目录还原，核对 restore.json 与 candidate/identity.json。完整步骤见 [交付指南](DELIVERY_GUIDE.md)。
