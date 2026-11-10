<a id="executor"></a>
# Executor：有限执行与回执服务

`kernelcat-executor` 0.8 分发包提供 `kernelcat_executor` Python 命名空间，负责效果准入、预算预留、执行回执和恢复。Gateway 与 Journal 是这些操作的原始管理方。导入包不会打开状态，也不会连接模型或设备。

安装统一按[根 README](../../../README.md)操作。产品启动、任务输入与配置见 [Agent 使用说明](../../../kernelcat/README.md)；本包不提供独立的 Agent 产品命令，也没有 `kernelcat` 兼容命名空间。

## 如何使用

日常任务由 `apps/agent` 的 Node 入口启动，Worker 通过有限 RPC 调用本包。自动跨 Trial 的执行由 LoopX 准入，获准 Trial 内的模型、工具调用和子 Agent 由 Pi 推进。Executor 不另起 Python 助手循环。

嵌入式调用方使用 `kernelcat_executor.operations.session.ExecutionSession`。构造时须注入已有 Gateway、Proposer、工具及策略，具体配置和装配由 `kernelcat_worker.configuration`、`kernelcat_worker.bindings` 管理。以下是 API 调用片段，`gateway`、`proposers`、`skills`、`task` 均须由可信调用方先准备，不能直接复制为完整程序：

```python
from kernelcat_executor.operations.session import ExecutionSession

session = ExecutionSession(
    gateway=gateway,
    proposers=proposers,
    skills=skills,
    cwd=workspace_directory,
)
try:
    result = session.run(task)
finally:
    session.close()
```

`workspace_directory` 是显式 `Path`，决定 Pi 子进程的工作目录；默认使用调用进程 cwd。返回值、事件订阅、输入队列与生命周期 API 以 [session.py](src/kernelcat_executor/operations/session.py) 为准。运行前还须配置已安装的 Pi Worker 根：`pi_worker.root` 或 `KERNELCAT_PI_WORKER_ROOT`；源码 checkout 之外没有隐式位置推断。`run(retry_model=...)` 只向已获授权的嵌入式调用方开放；Node 产品目前没有等价的手动重试选项。

`python -m kernelcat_executor` 会明确失败并指向 Node 产品入口。Worker 管理命令为 `kernelcat-admin`，旧的产品 `run` 在读取配置或打开状态之前就会拒绝。不要将它作为产品入口或绕过启动准入。

Worker 启动需要与实际部署一致的独立 `ExpectedPythonModules`，绑定 Worker、Executor、Knowledge、Ingestion 四个包，并与配置中的 `python_modules` 一致。启动报告记录实际观察，不授予执行权限。资源根、配置、报告及状态路径要求见 [Worker 说明](../../apps/worker/README.md)和 [Python 模块绑定合同](src/kernelcat_executor/contracts/python_modules.py)。安装、可选原生依赖和检查命令统一由根说明及 [CI](../../../validation/DELIVERY_GUIDE.md)维护。

## 模型输入和工具效果

外部 Context 请求将预期共享预算带入原 Journal 的准入事务。额度、已用量或预留发生变化时，在记录新模型意图前返回冲突；已有回执及 UNKNOWN 对账优先处理。供应商准备工作在账本锁之外进行。已知路由失败后，有限响应只刷新已准入 ContextSource 的预算，由 Node 重新编译下一条已授权路由的输入。

原生 Harness 接收 Pi 实际观察的 system prompt、文本/工具消息和工具声明。它校验受保护 ContextSource 前缀、当前 Gateway 工具定义、支持的消息形状及总字节上限，在发送前保留精确请求。工具声明元数据由固定 Node 适配器注入并记录摘要，不能自行增加 Gateway 权限。受保护 JSON 前缀按数值语义比较跨运行时的等价数字写法，原始 prompt 字节仍完整保留。

一次原生调用取得持久化的零积分 Gateway 意图和有效 permit；每个嵌套 MCP 调用仍经过原权限、参数、预算及回执检查。Host 分配有界序列身份；精确重复返回已有回执。脚本失败保留已完成效果，进程丢失或嵌套效果 UNKNOWN 保留未结算工作并阻止续接。这里没有外部 MCP 配置发现、自动重试或另一套记账系统。

## Run、Trial 和账户

托管 Run 使用 `state.runtime.RuntimeJournal` 和 `RuntimeStore`：前者是同一加锁 JSONL 管理方的版本隔离实现，后者由 Worker 绑定一个 Run/Trial/Session/generation/lease epoch。只有显式 `account_create` 可以注资。旧账本和托管账本拒绝交叉打开；账户绑定 Journal 规范路径，复制账本到另一条路径会被拒绝。系统不自动迁移账户或清空预留。

`operations.runtime.RuntimeOwner` 通过类型化的 Journal、artifact、bound-state 端口执行有限准备、配置及来源决策。Worker 注入实际实现和工作区 materialization 回调。不可变文件存储在 `capabilities`，纯 JSON/Context 校验在 `contracts`；Profile-aware Journal 位于 `state`，`profiles.store.AgentJournal` 仍导出同一类及其原激活权限。

准备先记录意图，再写不可变产物。Freeze 检查精确 TaskContract 源码、规范 ProgramBundle、原始根来源、Controller 图、当前配置/Profile/VersionLock 与账户 revision；按已观察的资源及截止事实授予不同的 Branch-control 和 Session-workspace lease，并原子续期或撤销。两个 Run 可固定同一原始根并共享账户积分，各自保留策略、Profile、Run/Trial 上限。领域积分复用原准入和事件记账函数。

托管父模型请求必须带生成的 ContextCommit。精确 UTF-8 输入、类型化 Context 引用及 revision、初始 Capsule 和模型预留在同一 Journal 事件中采用。Harness 输入涵盖实际 system prompt、消息和声明；普通输入涵盖已接受 ContextSource。最终供应商请求仍由原管理方保留。每个 Turn 固定准入后的 Session 与历史 Trial，缓存结果不会采用未使用的新输入或再次计费。

准入和条件启动都检查当前 control/generation/epoch。停止或替换仅取消尚未启动的工作，实际费用为零；已启动或 UNKNOWN 工作继续预留并隔离替换资源。迟到结果只能结算原操作，不能推进当前 Context 或释放新 lease。过期不等于远程停止确认。事务内重放之后再读时钟，避免锁等待保留已过期权限；已知采用/控制回执的重放优先于这些实时检查，且不续期。

没有子效果意图且尚未 checkpoint 的原生回复，在所选 Context 中按完整交换省略，以便原已知模型回执继续处理封存调用；未决效果或损坏的完成引用仍会阻止投影，不伪造工具结果。

## 候选产物与完成

生成停止后，显式准入的有限操作在 Journal 事务之外快照实际拥有的工作区字节，复用和采用时均校验不可变内容。采用检查 Run/Trial CAS、快照原 Trial/parent 和不可变来源；后来的工作 Program 不能改写祖先。扫描失败按路径及原因拒绝整个快照；非阻塞描述符打开在读取前拒绝 FIFO/特殊文件，并保留 nofollow/类型检查。

`CANDIDATES_READY` 只证明候选字节来源，不能证明正确性、计时、奖励或发布。RuntimeState 暴露原 materialization 请求、状态、结果和候选采用请求，用于回复丢失后的对账。`bind_finalization` 仅在生成停止、lease 有效且无未决效果时，取得原任务锁和当前 CAS；它不创建 Pi 或生成权限。最终完成只释放当前两个本地 lease。没有原生成结果时，有限完成明确返回未知的生成资格，不补 checkpoint、不绑定 Actor、不释放账务/分配。

每次托管事务重放 JSONL 历史，时间与内存为 O(E + 保留对象/输入字节)，所选图最多 16,384 对象、4,096 引用；重放维护对象索引，不反复扫描完整历史。这是本地 Linux/macOS 文件系统协议，未承诺 NFS、多主机、索引数据库或跨系统 exactly-once 执行。

## 多 Trial 与 Knowledge

显式 `multi_trial: true` 创建新 v3 账户；可不启用 Knowledge。若固定 Knowledge authority，每次 Trial freeze 前必须安装对应 capture。V1/v2 保留原 header/命令，不自动迁移；实际 v3 header 由 `contracts.knowledge_fences.MULTI_TRIAL_HEADER` 定义。新 Run 通过 `Run.root_program_id` 复用精确原始根，保留 `Program.run_id` 的来源，与之后 Trial 的 parent 分开。

`RuntimeOwner` 的有限接口包括 `prepare_trial`、`freeze_trial`、`prepare_knowledge`、`install_knowledge`、`knowledge_request(run_id, trial_id)`、`preparation(run_id, trial_id)`。Capture 准备返回含原始账户/control revision 的完整准入请求，再由可信装配调用 Knowledge。安装先在事务外验证规范回执字节及引用范围，再提交紧凑绑定。

Capture 使用单个 `RuntimeArtifacts.reference(raw, "knowledge-capture")`；`ref_id` 与 `record_id` 均为 `knowledge-capture/<sha256 hex>`，`digest_scope` 为 `exact UTF-8 knowledge-capture bytes`，locator 必须在已配置不可变产物根内。Capture 元数据上限 16 MiB，ProgramBundle 上限 8 MiB；依赖成员保留在回执内，不扩展受保护 Context 引用。生成的 Knowledge validator 用结构哈希键处理 `uniqueItems`，保持 JSON 的对象键无序、数组有序、数值相等和布尔值区分语义，避免对 16,384 成员做两两比较。

Trial 准备检查当前 stamp、同根保留 parent、Run 无未决效果及独立 StopRun 终态，在 `runtime_trial_control/v1` 中先隔离旧的两个 lease，再做产物工作。Deadline 重放后仍有效；替换未完成命令须显式 supersession 和新身份。Freeze 检查完整保留图及新增 Trial、Proposal、Branch、Session、两个 lease、Trial 预留及 memory revision。精确已知 v3 freeze 重放返回 `freeze_replayed: true`；普通状态读取不持久化该观察，可信装配用同事务结果避免再启动 SDK 生命周期。

重放保留各 Trial 的 Task、不可变 parent、Context、control、lease 和预算；共享一个 Run 图、聚合预算及当前指针。效果始终绑定原 Trial；首次终态物理 Journal sequence 只推进该 Trial 的反馈 cursor。历史 Actor 不能准入或启动效果，原始迟到结算仍可进行。父 ContextSource 和只读 delegate 保护同一已安装知识绑定，四个知识读工具可加入 delegate 读集。知识选择、保留、对账与工具实现仍由 Knowledge 管理。

## 代码与验证

`operations` 管理有限操作，`state` 管理回执和预留，`capabilities` 实现效果，`profiles` 管理 Profile 提案、证据与存储。原生编译/运行位于 `backends/ascend`；独立评估协议位于 `evaluation`。

从交付根目录执行 [回归命令](../../../README.md#独立安装与开发)，覆盖正确性检查、工具准入、源码冻结、预算和恢复。设备结果见 [交付检查](../../../validation/DELIVERY_GUIDE.md)。
