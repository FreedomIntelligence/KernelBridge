# CANN 算子任务：Pi SDK 与 LoopX Goal

| task 文件 | 输入和输出 | 原生路径 |
| --- | --- | --- |
| [Vector 向量加](vector-add/task.json) | 两个 FP32 `[128]` → FP32 `[128]` | Ascend C Vector `Add` |
| [Cube 矩阵乘](cube-matmul/task.json) | FP16 `[128,128]` × FP16 `[128,128]` → FP16 `[128,128]` | Cube Matmul，FP32 累加 |

**这两个 task 原文件已通过正式 SDK 在线生成和真实 NPU 验收。** 平台为 Ascend910_9382、逻辑卡 1、CANN 9.1、torch_npu 2.10，模型为 DeepSeek Flash。两次会话正常 `finished`，独立验收均为 `accepted=true`。它们各覆盖一个固定形状、五个随机种子；没有据此宣称其他形状也正确或性能优于基线。详细证据见 [验收记录](../../validation/ACCEPTANCE_RESULTS.md)。

## task 文件表达什么

`goal` 就是传入 Pi 的初始 prompt；`contract` 描述算子语义、shape/dtype、设备、测试数据、误差和计时要求；`seed_files` 准备工作区。`solution.py` 初始是待实现占位文件，`reference/` 包含冻结的输入说明、参考实现，以及在当前平台验证过的原生示例。Pi 使用自身 read/write/edit/bash/grep/find/ls 工具写代码，调用 NPU 评测、Knowledge 和 receipt 等业务工具。

修改各目录的 `case.json` 后，在仓库根目录运行：

```sh
python3 -B examples/cann/make_tasks.py
```

这会同步重建 task、参考实现和评测请求，避免只改 prompt、没改测试。当前生成器支持这两个算子的固定形状和五个种子，不支持任意 distribution。改变 dtype、维度、布局、SoC 或 CANN 版本后，必须重新生成并评测候选；模板不保证自动适配所有平台。

## 普通任务：直接调用 Pi SDK

应用调用 `KernelCat.runTask({ task, onEvent, signal })`。它复用正式 CLI 的 Pi 会话、Gateway 和执行账本，不另建模型循环。可用随包的 runner 直接验证：

```sh
node examples/cann/run.mjs \
  --task /absolute/KernelBridge/examples/cann/vector-add/task.json \
  --config /absolute/trusted-npu-config.json \
  --python /absolute/KernelBridge/runtime/venv/bin/python \
  --expected-python-modules /absolute/KernelBridge/runtime/expected.json \
  --output /absolute/runs/vector-add-001
```

Cube 只需换成 `cube-matmul/task.json` 和绑定相应参考实现的 evaluator 配置。选择的 Python 必须导入 expected.json 所绑定的模块；使用解包部署时设置对应 `PYTHONPATH`。模型密钥通过配置指定的环境变量提供，不写进 task 或源码。

可信配置负责模型服务、预算和 evaluator。普通 evaluator 接收 `evaluate({candidate:"solution.py"})`，必须按该 task 的五组测试数据执行完整 six-source 编译、正确性与计时，并返回原始 Outcome；每个目录的 `evaluation-request.example.json` 是设备端请求模板。配置 `writable_paths:["solution.py"]`、`readonly_paths:["reference"]`。runner 会为这两处目录设置独立输出路径，并拒绝把 RSI 配置当作普通 evaluator 使用。

结束后有 `task.json`、`config.json`、`events.jsonl`、`state/events.jsonl`、原始评测输出、`result.json` 和 **`acceptance.json`**。独立验证器检查：

- Pi 正常结束，且不存在未决执行效果。
- 原始成功评测的候选哈希与最终文件相同，参考哈希、SoC 和误差策略匹配任务。
- 编译和正确性通过，存在二进制身份，100 个 NPU event 样本有效。

只有 `acceptance.accepted=true` 且 runner 退出码为 0 才算该示例验收通过。模型文字、旧候选的成功回执或单纯 `finished` 均不够。可以对已结束的目录只读复查：

```sh
/absolute/python -B examples/cann/validate.py --output /absolute/runs/vector-add-001
```

## Goal 模式：Pi 会话仍执行，LoopX 管理目标

优化目标使用 `KernelCat.createSession()` 和 `LoopXGoal`。原来的 task 继续表达目标和数据；平台再把它与冻结的 Program、TaskContract、预算和评测协议组成 Run/Trial。`task.json` 本身不是 Run manifest。先完成生成任务，再将有正确性凭据的候选作为优化 baseline，并把 task.goal 改成明确的性能目标和搜索预算。

可信 Goal 配置需要匹配该算子的固定 RSI adapter，提供分解、成对测量和独立确认；普通单臂 `evaluate` 不能代替这些协议。准备输入不会启动模型或设备：

```sh
python3 -B examples/cann/prepare_goal.py \
  --bundle /absolute/KernelBridge \
  --task /absolute/optimization-task.json \
  --config /absolute/trusted-rsi-config.json \
  --contract /absolute/task-contract.json \
  --baseline /absolute/verified-solution.py \
  --python /absolute/KernelBridge/runtime/venv/bin/python \
  --node /absolute/node \
  --expected-python-modules /absolute/KernelBridge/runtime/expected.json \
  --output /absolute/runs/cann-goal-001
node examples/cann/goal.mjs \
  --bundle /absolute/KernelBridge --input /absolute/runs/cann-goal-001 \
  --loopx /absolute/KernelBridge/runtime/venv/bin/loopx
```

这里调用一次 `LoopXGoal.advance()`，其中的 Pi Trial 按 `policy.max_steps` 执行，例如 40 次模型回合；**40 次模型回合不等于 40 次编译评测或 40 次 LoopX 调度**。自动跨 Trial 的产品接口见 [KernelCat Goal 文档](../../kernelcat/README.md#loopx-自动续接入口)。

远程长评测应复用 Worker 已有的 `RemoteJobs.submit/collect`：先保留原始句柄，设备端独立运行，断线后仅查询该句柄。本轮验收已接入该接口。SSH 失联不等于编译失败；不得在没有原始结果的情况下重复提交。接口不替代部署者的设备隔离、排队和鉴权配置。

## 数据与性能范围

五个种子为 1024–1028，输入是标准正态连续 CPU Tensor，再由执行器搬入指定 NPU。Cube golden 在 CPU 以 FP32 累加后转 FP16，容差 `atol=rtol=0.01`；Vector 为 `1e-4`。这些阈值由可信评测器实施，候选不能改写。

测试通过只覆盖当前固定形状。非整块尾部、空输入、非对齐 M/N/K、其他布局或 dtype 应扩展用例再验证。NPU event 测量绑定明确设备与 stream，不包含编译和数据准备；没有同设备成对复测时不宣称加速。附带模板和本地性能记录均不是 CANNBench 官方提交或成绩。

实际 40 回合优化产物另见 [Sigmoid 示例与来源](sigmoid-optimization/README.md)：同一 Pi SDK + LoopX 从合格原生起点优化，完整 20 用例通过；与上面的固定形状生成示例分开记录。
