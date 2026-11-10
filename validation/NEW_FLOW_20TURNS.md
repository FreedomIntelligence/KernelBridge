# Vector / Cube：新流程 20 轮回归

执行入口：`examples/cann/run.mjs` 默认 Goal 模式，task → LoopX Goal → Pi SDK → NPU 业务工具。DeepSeek `deepseek-flash`，Pi 1.5.0，LoopX 1.2.3；Ascend910_9382 逻辑 NPU 1，CANN 9.1，Torch / torch_npu 2.10。每个任务设置 `policy.max_steps: 20`。轮数从执行账本的 `__propose__` 按主任务统计。

## 在线结果

Vector Add 使用 FP32 `[128]`；Cube Matmul 使用 FP16 `[128,128]`，保持任务原始种子和容差。共完成两组 Vector / Cube 测试，即 80 个主模型回合；Cube v5 另有 1 个只读审阅子回合。

| 会话 | 主回合 | 提交实验 | 接受候选 | 原始 Goal 结果 |
| --- | ---: | ---: | ---: | --- |
| vector-add-v4 | 20 | 1，另有 1 次无变化调用被拒绝 | 1 | validation_failed：接受后又编辑，结束时留下未评测草稿 |
| cube-matmul-v3 | 20 | 2 | 0 | no_progress：保留初始实现 |
| vector-add-v5 | 20 | 3 | 0 | no_progress：一个候选正确但确认未接受，另两个正确性失败 |
| cube-matmul-v5 | 20 | 2，另有 1 次无变化调用被拒绝 | 0 | validation_failed：候选正确性/编译失败，结束时又留下草稿 |

v5 使用了第一阶段的资源持有修复。最后的 stream 工作区修复和候选冻结修复，在上述会话结束后做了针对性回归；原始会话的失败状态保持原样。四次在线运行没有产生一组 Vector、Cube 均通过最终验收的结果。

## 发现与修复

1. **托管 Goal 遗漏配置工具和权限。** `RuntimeService` 接入与普通 Worker 相同的 `configured_tools`、WorkspaceGuard、Pi workspace policy 和原生 session seed。实际模型请求包含 Pi 基础工具、IR/profiler 采集、解析、读取及展示工具；上下文包含冻结的通用 AGENTS 和任务附录。
2. **ACLNN 异步工作区使用了错误的分配路径。** 原 helper 过早释放工作区、未持有输入参数，并跨线程管理 CANN 线程局部资源。第一阶段修复持有期后，Cube 仍偶发返回历史结果：110 次测试中 10 次失败，输入回读正常。原生矩阵乘、关闭队列、同步执行三组对照各 110/110 通过。最终改为 `NPUCachingAllocator::raw_alloc_with_stream`，按真实提交 stream 分配工作区，回调内管理资源；保留异步队列。最终 Cube 默认 stream 110/110、非默认 stream 1110/1110 通过；同一 helper 重编译后的 Vector 110/110 通过。只替换 `RunOpApi` 或增加 `SetSysWorkspace` 均未解决，未采用这些改动。
3. **预算结束后快照使用未评测草稿。** RSI 快照改为读取执行账本当前保留的源码，工作区草稿和 Pi 历史原样保留。对四个真实会话重放选择逻辑，均取回正确的保留源码；Vector v4 取回第 13 轮接受的候选 `3176f4bf…`。新增回归覆盖草稿编辑、删除入口文件、缺失保留文件和越权替换参考文件。独立 validator 继续按源码与凭据验收。
4. **Goal 根因被后续状态查询遮盖。** Goal 尚未创建 frozen Run 就失败时，保存原始结果和观察状态，直接报告实际 Goal 状态。

远程测试适配器另修复了 CPU 日志解析排在 NPU 长编译后超时的问题：只对设备采集/测量持有 NPU 锁。早先超时请求按原句柄收集，保留原始 UNKNOWN 记录和后续远程完成结果。

## Agent 行为

原生 read/write/edit/bash、工具错误恢复、拒绝越界 shell、无变化实验拒绝均在实际会话中执行。IR、profiler 的采集与读取在首组会话执行，第二组 Vector 调用了 IR 采集。Cube 生成过未配套地址偏移的多核切分，以及类型不配套的 FP32 输出改动；正确性和编译检查分别拒绝了它们。20 个模型回合包含读取、诊断、修复和审阅，实际设备实验次数见表。

Vector v4 接受过一个成对评测候选；v5 的一次探索测量较快，但确认未接受。本轮最终 helper 修复后的测试验证正确性，性能结论需要使用该 helper 重新成对测量。

## 回归和归档

- Python 57 项、Node 25 项、TypeScript 类型检查；ACLNN 延迟队列回归检查输入、工作区、线程局部资源和 stream 身份。
- 最新 Vector / Cube 导出分别含 589 / 459 个文件，Pi 原生 session 分别 1 / 2 个；在新目录还原全部字节并用 Pi 构建上下文，通过哈希验证。
- `campaign.json`：按任务统计的轮数、每轮工具状态、实验、RSI 节点和测量。
- `exports/`：五次会话归档，含一次早期超时尝试，保留原始 JSONL、checkpoint 和 Pi HTML。
- `device-original.tar.gz`、`device-fixed.tar.gz`：源码、编译日志、正确性结果、成对计时、诊断和文件哈希。
- `snapshot-recovery/`：四次会话的保留源码、原始草稿与选择记录。
- `restore-checks/`、`host-regression/`：还原验证和主机回归。

源码父提交为 `23657e4`，实际测试源树及后续修复分别以补丁和哈希归档。模型凭据、私有 SSH/跳板实现保留在测试主机，交付包只包含执行结果和通用工具。

资源管理对照参考 Ascend [op_api_common.h](https://gitee.com/ascend/op-plugin/blob/master/op_plugin/utils/op_api_common.h)；运行结果来自本次实机记录。
