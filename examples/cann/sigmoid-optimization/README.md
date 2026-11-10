# Sigmoid：40-turn Goal 优化与一键复现

原实验从 `baseline.py` 的保守单核实现开始，由 DeepSeek Flash 经 Pi SDK + LoopX 执行 40 个父模型回合。最终 `solution.py` 使用 40 核、2048 元素分块和双缓冲；独立 Ascend C 文件位于 [native/](native/)，来源与哈希见 `provenance.json` 和 `native/sources.json`。

随包保留完整 `task.json`、`task-contract.json`、起始 baseline、最终实现以及 `suite/`：20 用例、数据生成器、参考、原始来源与许可证。平台为 Ascend910_9382、逻辑 NPU 1、CANN 9.1、torch_npu 2.10。

## 一条命令执行原实验流程

先按根 README 安装，在已配置 CANN 的 NPU 主机填写 `profiles/npu.example.json` 的模型、Python、Node 与 LoopX 路径，并设置其中 `api_key_env` 指定的凭据环境变量。运行：

```sh
node examples/cann/sigmoid-optimization/reproduce.mjs \
  --profile /absolute/profiles/npu.json \
  --output /absolute/runs/sigmoid-40turn-001
```

加 `--prepare-only` 可检查冻结输入。执行时自动建立 Goal、提交既有 managed SDK 会话、调用原 20 用例成对评测器、独立确认并保留全部事件、checkpoint、LoopX journal 和原始凭据。模型生成与设备计时产生本次新记录；原实验记录位于完整交付包的 `runs/sdk-goal-sigmoid-optimization2-40turn/`。

## 已有结果

全部 20 用例正确，覆盖 FP16/BF16/FP32、不规则长度、NaN/Inf 和输入保持。完整输出使用原 MERE/MARE 阈值。独立复测相对单核 baseline 为 **16.91×**；相对 Torch 为 **0.512×**。结果类型为固定测试集上的本地 NPU 成对测量，具体逐例值见 `performance.json`。

## 日志与复查

完整交付包 `runs/` 保留可直接打开的 Pi session HTML、原生 JSONL、SDK 事件、KernelBridge 账本和 LoopX 状态。`logs/` 提供包括失败修复在内的全部原始日志与远端编译/计时归档。文件 SHA256 由各层 manifest 索引。复查按 task、候选源码摘要、编译凭据、测量样本与 Goal validator 结果关联。

## 恢复之前的 session

```sh
bash restore.sh \
  --run /absolute/KernelBridge-delivery-20261109/runs/sdk-goal-sigmoid-optimization2-40turn \
  --output /absolute/restored/sigmoid-session
```

恢复脚本安装锁定依赖，验证全部文件哈希，并通过 Pi SessionManager 还原会话 ID、当前分支与完整模型上下文。加入 `--profile /absolute/npu.json --continue`，使用原最终候选和对话启动新的受控 Goal；`lineage.json` 记录关联。
