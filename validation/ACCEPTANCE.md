# 算子任务验收

## 功能验收

| 项目 | 要求 |
| --- | --- |
| 任务身份 | goal、case、参考、初始实现和运行配置冻结并记录哈希 |
| 基线 | 在指定设备环境中编译并通过正确性检查 |
| 候选 | 完整 Ascend C six-source 实现，编译成功 |
| 正确性 | 全部声明输入通过，输出 shape/dtype 和容差符合合同，输入保持不变 |
| 计时 | 使用目标 NPU event，保存样本及构建身份 |
| 源码绑定 | 最终 Program 与通过评测的候选字节一致 |
| Goal | 独立 validator 通过，Goal acceptance 完成，未决效果为空 |
| 归档 | manifest 哈希一致，可恢复原 session ID、leaf 和上下文 |

Vector Add FP32 [128] 和 Cube Matmul FP16 [128,128] 示例在 Ascend910_9382、CANN 9.1、Torch/torch_npu 2.10 上完成完整编译和五种输入正确性验收；最终评测各保存 100 次 NPU event 样本。随包 runs 提供对应的 completed Goal 与完整 session。

## 性能验收

性能任务先通过正确性，再在相同设备和输入合同下对 baseline 与候选交替测量，保留原始样本、源码和构建摘要，并执行独立确认。profiler 与 IR 用于解释执行特征；性能结论使用任务声明的计时协议。

示例结果与复现入口见 [性能示例](PERFORMANCE_EXAMPLES.md)。

## 验收文件

- acceptance.json：验收结果与未决效果。
- result.json：任务或 Goal 状态。
- candidate/identity.json：交付源码身份及 Program 关联。
- evaluations/：编译、正确性、计时和诊断输出。
- state/events.jsonl：执行账本。
- state/pi-sessions/：Pi 原生会话与 checkpoint。
- manifest.json：逐文件 SHA256 和关联索引。

修改 shape、dtype、目标平台或源码后，使用新任务运行重新生成相应验收凭据。
