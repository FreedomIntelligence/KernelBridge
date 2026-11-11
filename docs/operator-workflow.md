# 算子开发工作流与无 Torch baseline 迭代

默认 `./kernelagent` 仍是普通 Pi codegen。下面的工具仅在配置中显式启用
`operator_pipeline` 或 `operator_infra` 后注册；普通会话不会自行启动测评或上板。
工具走原有 Executor Journal、输入队列和共享预算，没有第二份可花费额度。

## 正确性、比较器与 incumbent

这三个身份必须分开：

| 身份 | 用途 | 无 Torch 时 |
| --- | --- | --- |
| oracle | 判定正确性，绑定语义、容差、完整 case 集和 checker 源码 | 独立 Python/C++ 朴素实现、高精度实现、官方 checker，或足够覆盖语义的性质检查 |
| baseline | 同环境、同测量配置下的性能比较器 | 可以为空，也可以是厂商库、外部实现或已冻结的旧版本 |
| incumbent | 最近一次满足当前验收条件并晋升的源码 | 首版完整正确性通过后建立；后续显式选它作为比较器 |

没有 Torch **不阻塞实现**。没有性能 baseline 时先做完整正确性验收，再
`pipeline_promote` 建立首个 incumbent；结果标记 `bootstrap: true`，`speedup: null`。
调用 `pipeline_baseline({"selection":"incumbent"})` 后实现新候选，再跑完整检查和
ABBA 配对。不能把候选与自身输出相等当作独立正确性验证，也不能把首版运行时间
写成相对加速比。性质检查只能证明声明并实际执行的性质；驱动作者负责 oracle 的充分性。

选择 baseline、改变 contract/oracle/suite/measurement 会使之前的检查或比较失效。
原始源码、报告和事件链仍保留。已有 baseline 时，完整正确性和正向完整配对是晋升条件；
不设置“预估收益够大才测”的门槛。逐 case 和家族退步单独返回，不能只看整体数字。

## Harness 可调用接口

| 阶段 | 工具 | 保留的身份与约束 |
| --- | --- | --- |
| A | `pipeline_bind` | task、oracle 源码、suite、measurement；初始 baseline 可空 |
| B/C | `pipeline_plan` | workload 家族、假设、反例、引用的诊断证据 |
| D | `pipeline_freeze` | 显式文件映射和来源，冻结源码 SHA256 |
| E | `pipeline_run`，stage `E` | 配置的 launch/collect 驱动；collect 必须使用原 handle |
| F | `pipeline_run`，stage `F` | build、semantics、full 全通过；精确 case IDs 和 oracle 哈希 |
| G | `pipeline_pair` | A/B/B/A 四次实际调用，检查环境、测量配置和完整 case 集 |
| H | `pipeline_promote` | 当前源码、当前比较器与验收证据一致 |
| I | `pipeline_run`，stage `I`；`pipeline_archive` | 原校准回执、源码 ZIP/hash、成功机制与失败条件；本地归档不等于发布 |
| J | `pipeline_run`，stage `J` | profiler、IR、ISA、模拟器或探针的原始产物；回到 C 修订假设 |
| 查询/切换 | `pipeline_status`、`pipeline_read`、`pipeline_baseline` | 原始句柄、按哈希验证的分页产物、冻结比较器 |

`pipeline_pair` 当前报告各 case 的样本中位数、等权几何平均加速比和家族汇总。
它不是 CANNBench 官方加权分数；官方校准结果通过独立驱动/回执保留。
诊断不能替代完整验收，模拟时间不能替代板上性能。

驱动在显式配置中提供 `kind`、`argv`、`files` 和可选 `timeout`；源码文件在配置冻结时
绑定哈希。无 shell 拼接。每次驱动从 stdin 读取一个 JSON：
`{source, role, contract, handle, binding, output}`，stdout 返回一个 JSON。
启动超时、响应损坏或进程退出时，持久化的 `pending_launches` 阻止换 action ID 再次
launch（切换候选或 contract 也不能绕过）。调用 `pipeline_status` 取得原始 run，然后
`pipeline_run({"stage":"E","driver":"collect-driver","recover_run":"原始run"})`。
此时 collect 驱动还收到 `recovery`，包含原输出目录、源码、contract、binding 和启动驱动
身份；它必须查回原任务并返回原 `handle`，不能重新提交。找不到原任务时继续保留未决状态。
这是可配置驱动协议；不会自动连接或重试任何远端设备。
`source` 包含冻结目录、文件哈希和整体 SHA；`role` 为 A/B 或阶段角色。
驱动自行实现具体编译器/设备/测量器，接口保留 stdout、stderr、request 和 receipt。
check 结果须包含 `checks: {build:true, semantics:true, full:true}`、完整 `case_ids`、
`case_count` 和 `oracle_sha256`；measure 结果须包含 `correct:true`、`environment`、
`measurement`、`cases: {case_id: [positive_microseconds, ...]}`。
参见 [可执行驱动](../examples/scatter/driver.py) 和 [接口验收](../validation/test_operator_pipeline.py)。
同一个 action ID 不会重复执行；启动不确定时先回收原任务，不能重新 launch 冒充恢复。

## scatter max/min

[examples/scatter](../examples/scatter/) 提供值输出的 feature-index `scatter(..., reduce="max"/"min")`：

- `torch_api.py`：通用 PyTorch wrapper，支持 dim、index 广播、dim_size、out、非连续输入；
  有限数输入契约，空 bin 为 0。这里的便捷 `scatter_max/min` 只返回值，
  不模拟 `torch_scatter.scatter_max/min` 的 `(values, arg_indices)` API。
- `native.cpp`：无需 Torch 的可编译 FP32 CPU 实现；`reference.py` 是独立分组 oracle。
- `driver.py`：12 个本地 case，覆盖 max/min、空输入、单元素、稀疏索引、重复值、
  高碰撞及宽特征；实际编译、运行、逐项检查和计时。
- `diagnostic.py`：实际 clang LLVM IR/汇编、PyPTO HTML 比较、CPU profiling CSV。
- `ascend_sources.py` 及 [scatter-max](../examples/cann/scatter-max/)、
  [scatter-min](../examples/cann/scatter-min/)：AscendC 候选与冻结任务，FP32 `[N,F]`、
  int64 `[N]` index、dim=0、固定 dim_size。设备编译、正确性和性能仍待 NPU 验收。

本地示例：

```sh
python3 -B examples/scatter/prepare.py --output "$PWD/.validation/scatter-dev"
```

将生成的 `pipeline-profile.json` 的 `operator_pipeline` 字段合并到你的模型配置，
用 `./kernelagent --config /absolute/operator-config.json` 启动。不要复制密钥；沿用
`api_key_env` 和现有本地 credentials。然后输入：

```text
读取 /absolute/scatter-dev/contract.json，用 pipeline_bind 绑定。
通过 pipeline_plan 分析 workload 和假设，再冻结 native.cpp。
先完整检查，无 baseline 时建立 incumbent，然后选择 incumbent 自我迭代。
用 IR 和 profiler 诊断，保留原始结果；本次只做本地验收。
```

## 复用已有 infra

在同一配置中显式添加：

```json
{
  "operator_infra": {
    "source_root": "/absolute/ops/infra",
    "read_roots": ["/absolute/ops", "/absolute/scatter-dev"],
    "drivers": {}
  }
}
```

可调用的 17 个工具：`infra_context`、`infra_design`、`infra_preflight`、`infra_index`、
`infra_compare`、`infra_forge_audit`、`infra_cannbench_query`、`infra_release_prepare`、
`infra_release_bind`、`infra_score_opportunities`、`infra_trajectory`、`infra_campaign_init`、
`infra_run_driver`、`infra_ir_render`、`infra_trace_clean`、`infra_trace_pc`、`infra_read`。
适配器冻结共享模块和诊断工具，索引写入当前 run，不改共享 runs/knowledge。
`infra_run_driver` 只接受配置过且输入已绑定哈希的 argv，不自建网关。

`start_engram_forge.py` 原脚本在顶层执行远端上传和跨工程改写，不能直接 import；
这里只保留其源哈希，用参数化的本地 campaign 初始化替代这些副作用。
`infra_cannbench_query` 仅 GET，凭据来自宿主 `CANNBENCH_TOKEN`；release 工具只准备和绑定
本地字节，不上传。DQ 评分敏感性适配器沿用原 DynamicQuant 20-case 格式，Forge 审计
沿用原 20-case campaign 格式；它们不是任意算子的统一评分器。

## fork 与子代理的使用边界

本次日志审计范围是 6 段主会话及 79 份关联子任务日志，识别出 5 条主会话 parent-fork
关系、254 次主会话 compaction、40 次 spawn 和 225 次 follow-up。79 份子任务日志
不能写成 79 次对话 fork；继承历史也不能重复算成独立实验。

这些记录说明任务跨很长上下文推进，且已有子任务被多次继续使用。基于此，分支提示强调
保留假设、已验收源码、失败条件和原任务 handle；不以“开了新分支”替代晋升或恢复。
自动 fork 关闭时，连续工具错误、多次 compaction 或有同测量配置证据的 plateau 会提示
`/fork`，不会擅自开分支。普通 codegen 的 fork 只分对话；显式隔离任务的历史 fork 同时
恢复已记录的源码。所有分支沿用原账本。

子代理配置、触发、resume 和 compact 用法见 [TUI 文档](tui.md)。
