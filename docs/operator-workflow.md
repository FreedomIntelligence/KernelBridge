# 算子开发工作流与无 Torch baseline 迭代

默认 `./kernelagent` 仍是普通 Pi codegen。下面的工具仅在配置中显式启用
`operator_pipeline` 或 `operator_infra` 后注册；普通会话不会自行启动测评或上板。
工具走原有 Executor Journal、输入队列和共享预算，没有第二份可花费额度。

启用 `operator_pipeline` 后，harness 根据封存工具表自动加载算子流程提示。
用户提出算子开发或优化任务时，模型先调用 `pipeline_status`，随后按每次回执中的
`workflow.next_actions` 推进 A→B→C→D→E→F→G→H→I；失败或退步时读取 J 的
诊断产物并回到 C，用户要求继续迭代时由 I 回到 B。用户不需要逐个输入阶段命令。
状态建议不替代模型决策，也不扩大授权；源码、完整正确性、配对和晋升约束仍由工具强制检查。

独立 launch、官方完整测评、官网校准及诊断能力取决于显式配置的驱动。
没有对应驱动时返回缺失条件，不会用本地检查冒充官方验收。没有独立 E 驱动的本地流程
可以由 F 直接构建和检查。未知执行必须收回原任务，不会自动重新上传或启动。

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
| D/E/F | `pipeline_evaluate` | 一次有界调用冻结、启动或恢复原任务、收集、完整检查；各内部阶段独立保留回执 |
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

### 一次评测与原任务恢复

实现候选后，优先调用 `pipeline_evaluate`，传入 `files` 和 `origin`；
可用 `launch_driver`、`collect_driver`、`check_driver` 显式选择 profile 中的驱动。
每类只有一个驱动时可省略选择；没有独立 launch 驱动的本地流程直接执行 F。
`wait_seconds` 限制本次等待，不保证设备会在期限内结束，也不允许替换原任务。
G 配对、H 晋升仍是独立步骤；该工具不会自动把正确性通过当成性能收益。

结果返回 `evaluation_status`、冻结源码哈希、`source_changed`、实际发生的阶段、
原始 `check_receipt`、具体错误反馈及有效后续动作。未改变源码时复用已有检查或原任务，
不靠新 action ID 重复启动。同一候选失败后应按原错误修改源码，再评测新身份。

正常返回 `pending` 时使用回执的 `continuation`，以 `resume_run` 续查原评测，
不再传替换源码。若外层调用本身崩溃或回执未知，仍必须先由宿主恢复原 Journal action；
`resume_run` 不绕过未知效应隔离。内部 F 已完成但外层未返回时可以核验并复用原 F，
其源码、原 admission、内部 manifest 和回执哈希必须同时匹配。

大结果保留紧凑反馈和 `pipeline_read` 入口。分页结果注明 `encoding`、总字节数、
文件 SHA256 和是否到达末尾；`next_offset` 按原文件字节计数。任意二进制或不完整
UTF-8 起始位置可返回 base64，不能把编码文本直接当原始诊断内容。
自动诊断是原 F/G 结果之外的独立反馈，不修改验收使用的原始结果。

显式设置 `max_model_turns` 时，模型请求附带 Journal 的 `goal_progress` 观察快照，
计入父代理、子代理、压缩、review 和失败/未决调用。剩余次数包含本次请求，
并发调用可能继续消耗；该快照不预留额度。未设置上限时不生成默认预算。
20 次模型调用不等于 20 次候选优化或上板实验。

新增工具需要重新 prepare 配置并启动新会话；已有封存 profile、旧试验和原回执保持原样。

### 项目指导与临时文件

`examples/scatter/prepare_npu_campaign.py` 为新任务加入通用 `AGENTS.md` 和配置明确的
`AGENTS.task.md`。父代理加载冻结的项目文件；clean operator 子代理由宿主将这两份
已准入只读指令及 SHA256 放入实际模型请求。子代理仍不继承父聊天；委托时应提供相关
错误摘录、源码身份和问题，不能假设它能读取父代理的任意回执路径。

clean task 的 `writable` 字段声明**候选运行源码集合**，用于冻结与验收。会话可在工作区
建立其他 helper、草稿和临时数据；这些文件随完整 checkpoint 保存、恢复，但不自动进入
候选源码包。候选真正依赖的文件必须属于声明的源码集合。当前六段 native 接口仅执行
冻结 `candidate.py` 中规定的源码字符串，不执行任意 workspace helper。

引用、任务文档和宿主读取的 agent 配置路径保持只读。隔离 bash 每次使用新的 snapshot：
退出码为 0 才写回通过授权的 regular files；非零退出或超时不写回。使用相对路径保存
后续仍需使用的文件，使用 `$TMPDIR` 存放单次命令临时文件；不要保存上一次 bash 的
绝对临时目录。当前 snapshot 限制为每文件 256 KiB、总计 16 MiB、4096 个文件，支持
二进制字节恢复。工作区权限与候选验收范围是两套不同约束。

### 按证据缺口联网检索

联网检索是独立配置的工具，不是模型已具备的隐含能力。在普通 codegen 或 CANN profile
顶层加入 `online_research`；clean NPU campaign 也接受该字段：

```json
{
  "online_research": {
    "provider": "brave",
    "api_key_env": "BRAVE_SEARCH_API_KEY"
  }
}
```

凭据从该独立环境变量或本地 credentials 中读取，不写进 profile、源码或模型上下文，
不能复用模型 API key。也可配置 `{"provider":"searxng","endpoint":"https://your-instance.example/search"}`，
或使用 `true` / `{"provider":"duckduckgo_html"}`；后者是无需 API key 的尽力服务，HTML
接口变化、限流或验证页面会明确返回失败，不保证搜索可用。
`prepare_npu_campaign.py --online-research` 是启用这一默认 provider 的便捷参数。
不配置时不注册检索工具。

另有显式的 `bing_rss` provider，使用公开 RSS 返回；它也是 best-effort 接口，不能保证
长期可用性或检索相关性。存在网络或 DNS 问题时，可显式配置 loopback HTTP CONNECT
代理和 `dns_resolver: "google_doh"`。每次仍校验公开目标 IP、TLS 证书与 hostname；
不读取环境代理、不绕过证书校验，也不在 provider 之间自动 fallback。CA bundle 从
指定运行时的可用系统 CA 或 certifi 验证、复制并绑定哈希，也可显式提供 `ca_bundle`。
具体字段与真实验证边界见 [online research](../examples/online_research/README.md)。

| 工具 | 实际行为 |
| --- | --- |
| `online_search` | 向配置的 provider 提交显式公开 query，保留真实 URL、摘要和回执 |
| `online_fetch` | GET 显式公开 HTTPS URL，保留原始响应和有界正文；不使用模型改写内容 |
| `online_read` | 按原 run/path 分页读取已保留的结果，不发网络请求 |

检索只发送模型显式提交的 query/URL，不自动附带聊天或工作区内容。网络访问限制为
公开 HTTPS，禁止凭据 URL、私网目标和不安全重定向；不用环境代理绕过这些约束。
read-only 子代理只在父工具表已准入这些工具时获得它们；analysis 子代理仍无工具。
web 内容是外部资料，不是用户指令、编译结果或设备证据。

指导在 API/语义、目标 SDK 支持、具体报错、结构假设或 profiler 字段存在缺口时触发
检索，优先官方文档与固定版本源码。它不要求每轮搜索，也不注入旧算子答案。
实现对照和未声称兼容的行为见[官方源码核对](architecture/operator-guidance-alignment.md)。

## 测评后的自动诊断

仅仅注册 `ir_collect`、`profiler_collect` 或 J 阶段驱动，并不意味着模型一定会调用它们。
需要确定性调度时，在显式的 `operator_pipeline` 配置中加入：

```json
{
  "diagnostics": {
    "on_failure": true,
    "on_regression": true,
    "required": false,
    "drivers": { "ir": "device-ir", "profiler": "device-profiler" },
    "scope": "npu_diagnostic"
  }
}
```

`drivers` 的值必须是同一 profile 中已配置、绑定文件哈希的 `kind: "diagnostic"`
驱动名。`scope` 必须与驱动原始结果一致；本地 scatter 示例应填写
`local_cpu_diagnostic`，不能把 CPU 产物标为 NPU。

- `on_failure`：F 的 build/semantics/full 未全部通过，或 F/G 驱动已知失败时执行诊断。
- `on_regression`：G 的整体配对无正向收益，或任一 case 回退时执行诊断。
- `required`：F/G 后必须有当前候选的 IR 和 profiler；缺失、失败、不可用、未知或产物被修改
  时阻止晋升。它不改变正确性或正向配对条件。

未提供 `diagnostics` 时仍为手动 J 阶段；普通 codegen 没有隐式上板。该策略覆盖
`pipeline_run(stage="F")`、`pipeline_evaluate` 的内部 F 与 `pipeline_pair`，独立的 `operator_check` /
`operator_evaluate` 不会被拦截。采用自动流程的 NPU campaign 应走这些 pipeline 入口。

驱动仍收到冻结的 `source`、`contract`、`binding`、`output` 和原 `handle`，额外的
`trigger` 保留触发测评的 run、F/G 阶段、原因和相同 binding。结果协议是：

```json
{
  "kind": "ir",
  "scope": "npu_diagnostic",
  "candidate_sha256": "request.source.sha256",
  "artifacts": { "kernel.ll": "实际文件SHA256" }
}
```

产物必须实际存在于 `request.output` 内，使用相对路径，且 SHA256、候选身份和 scope
全部匹配。`profiler` 使用同样协议；没有硬件或工具时可返回
`{"status":"unavailable","reason":"具体原因"}`，不能返回空成功标记。
F/G 的返回结果及 `pipeline_status` 显示每项诊断的 `completed`、`failed`、
`unavailable`、`unknown` 或 `blocked`，并给出原始 receipt 与读取路径。
主 agent 应通过 `pipeline_read` 读取这些产物，再以 C 阶段记录新假设。

每个 binding、源码、驱动配置和 scope 组合只自动采集一次；再次触发会验证原始 receipt
和产物哈希后复用，不重复编译或采 trace。改变候选或比较器/测量契约后重新采集。
诊断超时或进程中断时保留原 intent，后续调用返回原未决状态，不自动重派；F/G 自身
超时未决时也不再启动设备诊断。先检查原日志/handle 并回收原任务。自动诊断不会掩盖
原正确性/性能失败，也不会自动宣称已读懂 trace 或完成设备验收。

对于已经在原 NPU 进程内完成测评的 campaign，
[diagnostic_policy.py](../examples/operator_pipeline/diagnostic_policy.py) 提供同一策略的单次接口：
先用 `diagnostic_reason(policy, failed=..., regression=...)` 判断是否采集，再用
`assess_diagnostics(policy, evaluation, records, artifact_root, source_sha256, binding=..., handle=...)`
检查实际产物并把结果写进原 `result.json`。此接口不启动进程、不重新编译或重新提交；
支持已保留 IR 和同进程采 profiler。`satisfied` 仅表示诊断产物完备，不能替代正确性或性能判定。

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
