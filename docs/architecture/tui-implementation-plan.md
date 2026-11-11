# KernelBridge TUI 实现计划

日期：2026-11-10。核对基线：`ce12581`，本机安装的 `@earendil-works/pi-coding-agent` 为 `1.5.0`。

产品契约修订（同日，按用户最新要求）：先完成普通 codegen，Goal、Trial 与 NPU 验收仅显式启用。无参数启动直接进入完整 Pi InteractiveMode，在当前项目等待输入，输入后执行，完成后空闲；允许先建立被动宿主会话，禁止自动模型/工具执行；不自动发送 fixture、不创建 Trial、不执行设备动作。省略 credits/max_steps/Goal 上限表示不设限制，状态栏不显示预算。普通会话的 `/new` 清空对话并保留文件，恢复不重放已完成请求。下文原先“默认创建 Goal”和“/new 总是创建 Trial”的设计由本段替代；隔离试验模式继续保留对应能力。

修订（同日，按审计意见）：补充命令前置闸门与白名单、三条退出路径与 `on_detach`、`awaiting_user` 状态、两份存储的落盘差异、键入输入的接收路径、验收证据等级、评测完整性、本地连接安全，并按依赖关系重排阶段出口。

本文定义实现契约、改造顺序和验收条件。默认运行时采用 Pi Coding Agent SDK，界面复用 Pi 原生 `InteractiveMode`；LoopX 实现 Goal 策略；KernelBridge 管理输入、执行、隔离、预算、上下文策略与回执。Pi Durable 保留为单独的运行时迁移项目。

实现更新（2026-11-10）：已实现常驻宿主、共享 Pi runtime、持久输入/租约/Goal、真实流、上下文切换和证据投影。功能代码完成后开始编写测试。本地验收入口为 `validation/tui/run.py`，结果与覆盖边界见 [本地验收记录](../../validation/tui/ACCEPTANCE.md)。用户选择先完成本地实现与验收，真实 NPU 验收保留待运行；本次不执行设备提交、GitHub 发布或 ZIP 发布。

实现中的接口约定：新增任务式 SDK 使用常驻宿主，旧 manifest 式 SDK 保留有限调用兼容层；普通 codegen 的 `/new` 创建新对话，显式 Goal 也不隐式创建 Trial；只有隔离试验模式的上下文切换创建 Trial。历史账本始终保留；试验 `/import` 只导入当前产品档案中有源码检查点的原生会话。Python 宿主持有原 Executor scope，Node 端负责启动、客户端与 Pi worker。新增 Node 模块采用现有 `.mjs` 风格。

后续实现与本地验收：已补齐会话选择器与原宿主恢复、当前/历史 fork、语义压缩、Goal 上限与阻塞恢复、可配置社区子代理，以及显式 A–J 算子工具。无 Torch 的正确性 oracle、可空性能 baseline、incumbent 自我迭代、scatter max/min 和 IR/profiler 接口见 [算子工作流](../operator-workflow.md)。覆盖及限制见 [最新验收记录](../../validation/tui/ACCEPTANCE.md#operator-workflow-and-session-follow-up)。本地验收与设备验收分别记录，不宣称与其他 CLI 所有实现细节完全相同。

## 1. 交付目标

用户默认进入普通 codegen，在当前项目读写文件和运行命令。显式提交 task/profile 后可进行算子生成、编译、正确性检查、性能评测和修复。只有 `/goal OBJECTIVE`、`--goal OBJECTIVE`、`--goal-from-task` 或 SDK `sessionOnly: false` 创建持续 Goal。TUI、CLI、SDK 使用同一套宿主协议、执行工具和账本。

计划新增入口：

```sh
./kernelagent
./kernelagent "修改并验证当前项目"
./kernelagent --continue
./kernelagent tui --task /path/task.json --profile /path/npu.json --output /path/run
./kernelagent tui --attach <application-session-id>
./kernelagent tui --task /path/task.json --profile /path/npu.json --output /path/session-run --session-only
./kernelagent sessions
./kernelagent session status <application-session-id>
```

`--on-detach pause|continue` 决定关闭界面后活跃 Goal 的去向，默认 `pause`，语义见 5.3。

界面保留 Pi 的编辑器、流式对话、工具输出、补全、历史浏览和主题组件。KernelBridge 增加 Goal/Trial 状态、剩余预算、当前源码身份、NPU job 和已验收候选展示。业务工具按 profile 暴露，适用于通用算子开发。

## 2. 已有基础与需要补齐的部分

以下是本轮源码核对结果，均以仓库实现为准。

| 部分 | 当前实现 | TUI 所需改造 |
| --- | --- | --- |
| Pi 生命周期 | [`sdk.mjs`](../../source/apps/agent/src/harness/pi/sdk.mjs) 已创建 `AgentSessionRuntime`，执行 prompt 后 dispose | 拆出可持续持有、替换、恢复和关闭的 runtime 工厂；保留原一次性 runner 作为兼容适配器 |
| 进程与 IPC | [`pi-host.ts`](../../source/apps/agent/src/loopx/pi-host.ts) 启动一次性子进程，stdin/stdout 为 ignore；[`process.ts`](../../source/apps/agent/src/harness/pi/process.ts) 使用单请求 IPC | 增加交互 worker、常驻宿主、输入和控制通道；TTY 与结构化协议分离 |
| 模型流 | `sdk.mjs` 等待 `controller.model()` 返回完整结果后，才发出 Pi `done` 事件 | 将真实模型增量通过受控 provider 传到 Pi；完整消息、usage 与终止状态仍统一结算 |
| 产品 SDK | [`session.ts`](../../source/apps/agent/src/sdk/session.ts) 有 prompt/steer/follow-up；声明 `tokenStreaming/nativeFork/persistentNativeSession: false` | 让 SDK 成为常驻宿主客户端；逐项通过测试后再更新 capability |
| 执行账本 | [`journal.py`](../../source/packages/executor/src/kernelcat_executor/state/journal.py) 已有文件锁、原子事件、fsync、预算预留和不确定状态 | 在原账本扩展输入、Goal 控制、租约、Trial 与恢复绑定，复用现有执行权威 |
| LoopX | [`goal.ts`](../../source/apps/agent/src/sdk/goal.ts) 已有 Goal 绑定、pursue、暂停和恢复，并调用 LoopX 状态/配额接口 | 将宿主调度与 Goal 投影接到统一账本；明确现有 LoopX 状态文件的投影身份 |
| 原生 fork | [`session-fork.ts`](../../source/apps/agent/src/loopx/session-fork.ts) 使用 `SessionManager.forkFrom()`，保留用户约束与不可变检查点 | 绑定产品 Trial、工作区快照、租约和继承规则 |
| 自动干预 | [`round-control.ts`](../../source/apps/agent/src/loopx/round-control.ts) 能提出重置/回退建议；[`intervention-control.ts`](../../source/apps/agent/src/loopx/intervention-control.ts) 正式选择仅接受 agent 的 replay 请求 | 增加可配置、带证据和限额的 LoopX 自动 fork 策略入口 |
| 上下文管理 | [`context-cleanup.ts`](../../source/apps/agent/src/replay/context-cleanup.ts) 已投影旧工具结果；自动 compaction 与 replay 配置绑定，且 hook 保留原始消息 | 解耦清理、语义压缩、fresh-context reset 和源码回退，分别测试 |
| 指令加载 | `sdk.mjs` 设置 `noExtensions`、`noSkills`、`noPromptTemplates`、`noThemes`、`noContextFiles`，并用 `DefaultResourceLoader.agentsFilesOverride` 显式加载任务指令 | 这组开关作为 runtime 工厂的不变量，交互路径（含 `/reload`、`/trust`、会话替换）不得绕过；TUI/SDK 共用同一冻结的 AGENTS 文件集合及哈希，在状态页可查看 |
| 原生命令 | Pi `interactive-mode.js` 的 `onSubmit` 按字面依次匹配 24 个内建命令、`/debug` 与隐藏命令，再处理 `!`/`!!`，最后才进入 input 回调；流式输出中的提交走 `prompt(…, steer)`，压缩期间的提交进入压缩队列 | 在 `onSubmit` 入口加命令前置闸门：白名单、默认拒绝；所有键入文本在此取得宿主接收回执（见 4.1、5.1） |
| 退出路径 | `shutdown()`（`/quit`、Ctrl-D、Ctrl-C 退出、SIGHUP/SIGTERM）先 dispose runtime 再 `process.exit(0)`；终端已断开时 `emergencyTerminalExit()` 跳过扩展清理，以 129 退出 | 交互退出由闸门先通知宿主；宿主以连接断开或进程退出判定 worker 丢失，不依赖 `session_shutdown`（见 5.3） |
| 会话落盘 | Pi `session-manager.js` 用 `appendFileSync` 追加、不 fsync；首条 user/assistant 消息出现前不创建文件；assistant 消息在 `message_end` 落盘，早于工具执行 | 进程崩溃时可依赖 JSONL 边界，主机故障时不可依赖；执行意图自带 tool call 身份（见 3.3、4.3） |

低层有 checkpoint/fork 代码，并不等于产品 SDK 已暴露相应能力。第一阶段同时覆盖这两个层次。

## 3. 运行时与控制权

### 3.1 进程分工

```text
CLI / SDK / TUI 输入
        │
        ▼
ApplicationHost ── 持久输入队列、租约、Goal 调度、事件订阅
        │                         ▲
        │ 唯一执行授权             │ LoopX 下一步建议
        ▼                         │
一个 Pi worker：interactive 或 headless
        │
        ▼
KernelBridge Executor ── 权限、沙箱、预算、源码冻结、外部任务
        │
        ▼
Journal / receipts ── 发布执行结果与可重建的 Goal 投影
```

宿主唯一决定哪个 worker 有权运行。Pi 负责单个会话内部的模型与工具循环；LoopX 决定下一次尝试的任务和停止条件。不能同时让宿主、Pi continuation 和 LoopX 各自提交一个“继续”。

第一版复用原生 `InteractiveMode`，所以交互 worker 内含界面和实际 `AgentSessionRuntime`；它不是纯远程渲染客户端。常驻宿主持有产品会话、队列和执行权威。退出 TUI 后按 Goal 的 `on_detach` 在检查点处理（见 5.3），而非声称原生界面能无缝脱离正在生成的会话。

### 3.2 终端与进程接入

- 宿主独立于终端存活，使用本地 Unix socket 接收客户端请求。首发支持 Linux 与 macOS；Windows 使用 WSL2 同一实现。
- 本地连接安全：
  - socket 放在当前用户私有的运行目录（0700），socket 文件为 0600；权限不符时宿主拒绝启动。
  - 每个连接校验对端 uid（Linux 用 `SO_PEERCRED`，macOS 用 `getpeereid`），必须与宿主相同。
  - 客户端还要出示会话 token，用于拒绝过期客户端和误连；token 存在 0600 文件中，attach 时读取。worker 另用一次性启动票据。
  - 宿主崩溃后，新宿主先确认账本锁无人持有，再清理残留 socket；锁仍被活动宿主持有时，改为 attach 到该宿主。
- 前台启动器向宿主申请一次性 worker 启动票据，启动直接继承当前 TTY 的交互 worker。worker 连接宿主、核对身份并获得租约后才能运行。启动器负责承接终端，调度权仍属于宿主。
- headless worker 由宿主启动；同一产品会话同一时刻仅有一个执行租约。其他连接可以观察、提交输入和请求接管。
- 交互 stdin/stdout 只服务终端。模型、工具、控制、日志使用独立 IPC/socket；不在 stdout 混入协议 JSON，不由父进程转发终端字节。
- 凭据由宿主/执行端读取。子进程使用受控 provider 与最小环境；客户部署直接配置自己的模型端点和 NPU profile，不引入开发环境的 gateway/proxy。

### 3.3 唯一事实来源

KernelBridge Journal 保存副作用、预算、输入接收、Goal 定义和控制事件、Trial 边界、租约变更及结果关联。Pi JSONL 保存原始对话树和上下文条目。LoopX 进度、最佳候选与完成状态按账本游标投影，缓存可以删除重建。

一次执行固定为：意图与预算预留原子落盘 → 执行 → 结果与预算结算原子落盘 → 对外发布。不同存储之间通过稳定身份与恢复规则关联；不会把两个独立文件的写入描述为同一事务。

两份存储的持久性不同：
- Journal 每次提交都 fsync。
- Pi 1.5.0 的会话文件用 `appendFileSync` 追加、不 fsync，首条 user/assistant 消息出现前也不创建文件。进程崩溃时，assistant 的 tool-call 条目在工具执行前已经写入；主机故障或断电时，末尾条目可能丢失。

因此恢复调用边界以 Journal 为准。执行意图记录 `pi_session_id`、assistant `entry_id`、`tool_call_id`、工具名和完整参数（参数过大时记录摘要并单独存档），不依赖 Pi JSONL 是否落盘。

权限/审批决定绑定原 action、参数摘要、策略版本和工作区身份入账；同一动作恢复时复用原决定。参数、作用域或策略版本改变时重新准入，历史许可不扩展到新的动作。

## 4. 输入、事件与恢复契约

### 4.1 输入进入一条持久队列

每个 prompt、steer、follow-up、控制命令、LoopX 调度建议都有 `input_id` 和内容摘要。提交返回持久接收回执；重试相同 ID 与内容返回原回执，相同 ID 不同内容报冲突。

输入状态为 `accepted → queued → applied → completed|interrupted|failed`。记录 `accepted_seq`、`applied_seq`、实际生效的 task version 和执行边界。接收不代表已经进入模型上下文。

- prompt：在空闲边界开始；忙时由用户明确选择 steer 或 follow-up。
- steer：当前工具批次结束后的下一个模型边界生效。
- follow-up：当前 run 完成待处理工作后生效。
- task/验收条件修改：生成新 task version；已提交的评测继续绑定旧版本。
- Goal pause、取消、接管可以在长工具执行期间被宿主接收。控制消息不能排在长评测的同一阻塞 RPC 后面。
- 键入的文本在命令闸门处取得接收回执（见 5.1）。这一个点覆盖 Pi 1.5.0 的三条提交路径：空闲时的普通提交、流式输出中的 steer、压缩期间的排队。闸门同步等待回执，超时可配置；宿主不可用或超时时明确拒绝，文本留在编辑器，不在本地先收下。
- 取得回执后，同一段文本带 `input_id` 交回 Pi 原有分支处理，界面只渲染一次用户消息。input hook 记录该 ID 进入 Pi 的时刻。Pi 1.5.0 的扩展 `message_end` 先于 `SessionManager.appendMessage`：回调只追加输入 ID 标记，下一模型准入边界核对随后落盘的 user entry 并 fsync 后，才将输入标为 applied，记录对应 `entry_id`。
- 宿主驱动的输入（SDK、CLI、LoopX）由宿主带 admission 上下文调用 `session.prompt`；input hook 识别该上下文后直接消费原 ID，避免二次排队。

### 4.2 身份和事件

事件最小关联字段：

```text
application_session_id, goal_id, trial_id, pi_session_id, entry_id
input_id, action_id, tool_call_id, eval_job_id
lease_epoch, task_version, source_sha256
initiator = user | agent | loopx
channel = tui | cli | sdk | policy
decision_id, parent_source_sha256, measurement_profile_version, evidence_ref
evaluator_sha256, evidence_tier
```

还未产生的 job/回执 ID 由后续事件关联，不回写历史事件。事实事件使用账本游标；流式 token/tool-output 增量使用独立流序号和 generation ID。增量可节流、合并，终态和回执不得丢失。慢客户端只能影响自己的订阅，不能阻塞执行或导致重跑。

`state/events.jsonl` 是执行账本。`events.jsonl` 是产品事件导出，必须能由账本和 Pi sessions 完整重建；流式增量和界面事件不写入它。两者的身份关联在 manifest 中声明，避免出现两份预算权威。

### 4.3 崩溃恢复

| 中断位置 | 恢复动作 |
| --- | --- |
| 输入接收后、应用前 | 消费同一 input ID，保持原队列顺序 |
| 工具调用准备后、执行前 | 从已持久化的原始 assistant/tool-call 边界恢复，再查询 action 状态 |
| 结果已写账本、Pi 未记 tool result | 向原 tool-call ID 补交同一回执；不重复执行 |
| Journal 有意图或结果，Pi JSONL 缺对应 assistant 条目（主机故障截断） | 以 Journal 意图为准恢复调用边界。已有结果时，追加一条标明 recovered 的宿主消息，写明原 tool call、参数摘要和原回执；无结果时按 action 状态核查。不伪造 assistant 消息，不重新执行 |
| NPU 已接受提交、本地未收到 job ID | 按提交前落盘的幂等键查询；远端确认未提交后才允许提交 |
| 无法确认 NPU 是否接受 | 保留预算预留并进入 reconciliation；界面显示核查状态 |
| Pi worker 崩溃或无清理退出 | 宿主由连接断开或进程退出发现，不等待 `session_shutdown`（终端断开时的 129 紧急退出不会触发它）；关闭旧执行租约，核查副作用与最后完整 checkpoint，再按 `on_detach` 恢复原 Trial |
| 宿主崩溃 | 取得原账本锁、核查活动进程和任务，递增租约版本，再恢复调度 |

外部效果开始前，Journal 意图必须已包含 3.3 列出的调用身份，并已 fsync。不能仅有 NPU intent，却没有可关联的会话调用。设备端需按 submission key 去重或找回任务；TUI 重连不会触发重新提交。

租约失效只拒绝新执行，不能假设已启动的 bash 自动停止。切换 workspace writer 前，原有本地写操作必须完成或确认终止；已提交的远端评测读取冻结快照，可以独立继续。

## 5. Pi 原生界面适配

### 5.1 复用与改造方式

从 `sdk.mjs` 提取 `createGovernedRuntime()`，供交互和 headless 两个 driver 使用。两条路径共用 provider、原生工具 operations、业务工具声明、AGENTS 加载、context hooks、checkpoint 和账本绑定。

界面业务能力通过受控扩展注册状态栏、命令和结果渲染。键入文本在命令闸门取得持久接收回执，Pi input hook 负责标记应用（见 4.1）；`!`/`!!` 使用 `user_bash` 的 operations/result 扩展点进入执行器。

原生内建命令在 `onSubmit` 中先于 input hook 处理，因此不能仅注册 `/goal` 和 input hook 就宣称入口统一。做法是在 Pi 锁定版本上增加一个命令前置闸门。补丁只在 `onSubmit` 入口插入一次调用，由闸门按白名单决定每段键入文本的去向：

- 已适配的命令：交给宿主，或按 5.2 的语义执行；
- 普通文本：取得接收回执后交回 Pi 原分支（见 4.1）；
- 白名单之外的命令，包括隐藏命令和升级后新增的内建命令：拒绝并提示，不执行。

白名单按阶段开放，见 5.2 的"启用"列。补丁作为显式版本化文件管理；安装时核对 Pi 版本与源码摘要，不符就拒绝进入交互模式；不修改运行中的私有方法。

第一阶段验证闸门与 `InteractiveMode` 的兼容性，后续阶段沿用同一实现。升级 Pi 时对比 `BUILTIN_SLASH_COMMANDS` 与 `onSubmit` 中的字面命令匹配；出现差异时测试失败，必须先更新白名单和 5.2 的语义表。

### 5.2 命令的明确语义

| 操作 | 产品行为 | 启用 |
| --- | --- | --- |
| 普通输入、steer、follow-up | 先由宿主持久接收，再按边界交给 Pi | P0 |
| `!command` / `!!command` | 都经过同一 bash 执行器；后者只改变是否进入模型上下文，执行账本仍保留；标记 `initiator=user` | P0 |
| `/quit`、空输入 Ctrl-D、Ctrl-C 退出 | P0：记录退出；有未结算效果时拒绝退出，或停止调度后干净关闭。P2 起按 `on_detach` 处理（见 5.3） | P0 / P2 |
| `/detach` | 关闭界面并让活跃 Goal 在后台继续，相当于本次按 `on_detach=continue` 处理 | P2 |
| `/goal` | 展示目标、任务版本、Trial、预算、最佳已验收候选、未完成 job、`on_detach` 与调度状态 | P1 |
| `/goal pause` / `/goal resume` | 记录控制事件；暂停停止后续调度，恢复按原状态继续；`resume` 同时解除 `awaiting_user` | P3 |
| `/goal clear` | 暂停后续调度并解除当前会话的 Goal 绑定；保留目标历史、预算、源码与回执 | P3 |
| `/goal 目标内容` | 与 Codex CLI 一致，新建 Goal 或提交目标版本变更；活动任务修改生成新版本 | P3 |
| `/goal on-detach pause\|continue` | 修改关闭界面后的去向，记录控制事件 | P2 |
| `/compact` | 当前 Trial 内压缩，模型调用经受控 provider 计入预算；不更换源码，不清零预算 | P1 |
| `/context clean` | 清理可证明冗余的工具输出投影；原始 JSONL 和完整证据保留 | P3 |
| `/context reset` | 新建上下文；普通项目保留文件，隔离试验才冻结源码并创建 Trial | P3 |
| `/fork` | 默认从当前 entry 分支，可指定历史 entry；保留父关联和账本，只有隔离试验恢复匹配源码并创建 Trial | P3 |
| `/clone` | 当前上下文分支，与 `/fork` 使用相同的宿主与源码约束 | P3 |
| `/tree` | P1 只读浏览原对话树；P3 起，选择历史位置继续执行时转为受控 fork，不让旧上下文配上当前可变源码 | P1 / P3 |
| `/resume` / `/new` / `/import` | 恢复原宿主/原会话、新建对话或导入档案；普通项目不隐式创建 Trial，恢复不重放已完成请求 | P2 |
| `/model` / `/thinking` / `/scoped-models` | 只能选择宿主 profile 允许的配置，`/scoped-models` 不能把 profile 之外的模型加入轮换；记录配置版本，仍使用受控 provider | P1 |
| `/settings` / `/reload` / `/trust` | 显示及重载准入配置；`/reload` 重新应用 runtime 工厂的冻结资源集；不能通过界面增加未声明的插件或放开工具执行权限 | P1 |
| `/export` / `/copy` / `/name` / `/session` | 复用展示操作；文件导出进入明确的输出目录，敏感配置脱敏 | P1 |
| `/changelog` / `/hotkeys` | 只读展示，原样复用 | P1 |
| `/login` / `/logout` | 转到宿主凭据配置；模型密钥不写入 Pi 子进程或 session | P1 |
| `/share` / `/bug` | 会把会话内容发往外部，产品构建默认禁用；仅当 profile 显式开启时可用，届时先显示待发送内容并由用户确认 | 默认禁用 |
| `/debug` | 原生实现把完整消息 JSONL 写到 `<agentDir>/pi-debug.log`，位于运行目录之外且未脱敏；默认禁用，开启时改为脱敏后写入 `logs/` | 默认禁用 |
| 隐藏命令、未知命令、升级后新增的命令 | 拒绝并提示 | — |

主题、输出折叠、编辑快捷键、滚动和补全继续使用 Pi 原生实现。状态栏的预算、job 和验收状态来自宿主回执，不解析模型自然语言来更新。

### 5.3 中断、退出与接管

Goal 的调度状态分为 `active`、`awaiting_user`、`paused` 和终态。

- Esc 取消当前生成并停止该输入的自动 continuation；已提交的评测继续并结算。Goal 进入 `awaiting_user`：LoopX 不再自动调度，直到用户提交新输入（按 steer/follow-up 规则生效后回到 `active`）或执行 `/goal resume`。状态栏显示这一状态。
- `/goal pause` 是持久暂停，只能由 `/goal resume` 解除。两种状态下，进行中的效果都独立核查和结算。
- Ctrl-C 在生成期间采用当前操作中断语义，中断后 Goal 状态与 Esc 相同；空闲时保留清空输入/退出交互的体验。由 Ctrl-C 触发的退出与 `/quit` 走同一退出拦截。
- 关闭界面后的去向由 Goal 的 `on_detach` 决定，取值 `pause` 或 `continue`，默认 `pause`。原因是终端关闭时无法询问用户，后台继续消耗预算和设备必须是显式选择。
  - 设置方式：创建 Goal 时用 `--on-detach`，之后用 `/goal on-detach`；`/detach` 只对本次生效。
  - 状态栏常驻显示当前取值；`/quit` 和 Ctrl-D 退出前再提示一次取值与剩余预算。
- 退出有三条路径：
  - 交互退出（`/quit`、Ctrl-D、Ctrl-C）：闸门先通知宿主，再执行原生 `shutdown()`；
  - 信号退出（SIGHUP、SIGTERM）：原生实现 dispose 后退出，扩展清理可能来不及通知宿主；
  - 终端已断开：`emergencyTerminalExit()` 跳过扩展清理，直接以 129 退出。

  因此宿主统一以 worker 连接断开或进程退出为准，不依赖 `session_shutdown`。检测到后，宿主冻结新调度，完成已知结果入账，确认本地写操作状态，撤销旧租约。`on_detach=continue` 时启动 headless worker；`pause` 时 Goal 转为 `paused`，保留检查点。
- `awaiting_user` 在界面关闭后保持不变，headless worker 不会替用户解除它。重新 attach，或通过 CLI/SDK 执行 `/goal resume`，才会继续。
- 无 Goal 的普通会话在退出后保留 checkpoint，等待下次 attach，不自动延长任务。
- 重新 attach 时，先停止 headless 的新 admission，在安全边界保存状态并释放租约，再授权交互 worker。TUI 关闭和 attach 均可能中断正在流出的回答；恢复从最后确认边界开始。
- 所有模型/工具 admission 校验 `lease_epoch`。旧 worker 即使仍存活，也不能凭旧租约继续发起执行。

## 6. 自动 fork 与上下文管理

这些能力是本次 TUI 的正式工作流，不依赖 Pi Durable。

| 动作 | 决策方与执行方 | 保留内容 |
| --- | --- | --- |
| 输出清理 | KernelBridge 生成请求投影，Pi context hook 应用 | 用户约束、近期工具交换、失败/未完成结果、完整原始日志 |
| 语义压缩 | Pi 原生 compaction；KernelBridge 固定事实字段并预留模型预算 | Goal/task version、当前源码身份、有效回执、待完成任务 |
| 新上下文 | 用户、agent 或 LoopX 提议；宿主在稳定边界执行 | 当前源码快照和账本交接材料；历史 session 继续可读 |
| 历史候选回退 | 提议明确 node/source ID；KernelBridge 核验后创建 Trial | 被选源码、原测量条件、父上下文位置及后续用户约束 |

具体改造：

1. 将自动 compaction 开关从 replay 插件配置中独立出来；普通 Pi 会话也能启用原生压缩。
2. 先做证据化工具输出清理，再由 Pi 对剩余历史做语义压缩；删除当前“将全部原消息保留为 compaction 起点”的替代行为。
3. 压缩模型调用也走宿主预算许可与计费；不会因为进入摘要流程就获得另一条 provider 通道。
4. 自动 fork 配置包括启用条件、最小观测窗口、冷却间隔、最大 fork 次数。测量未知时先检查评测，不能判成优化停滞。停滞判断使用第 7 节的证据等级，单次结果的起伏不算停滞，也不算进展。
5. LoopX 的自动干预提交持久调度建议，使用 `initiator=loopx`；agent 的 replay 请求使用 `initiator=agent`。两者经同一 admission 后执行。
6. 跨 Trial 新建上下文，Trial 内连续使用原会话。新的 Trial 不重置 Goal 总预算、总轮数或历史失败记录。
7. 交接材料由账本生成：目标版本、最佳有效源码及回执（按第 7 节等级区分单次最高、已验收最佳和平台成绩）、已尝试方向和适用条件、未结束 job、剩余额度。模型摘要只能补充解释。

保留现有 `goal.max_trials` 与每 Trial 的 `policy.max_steps`，补充 Goal 级 `max_model_turns` 和 `max_forks`。这些限制作为任务配置进入 KernelBridge admission；LoopX 读取执行端的剩余额度来制定策略，不维护另一套可消费余额。

已有 RSI branch 与产品 Managed Trial 的映射必须在同一控制事务内建立；不能只创建新的 Pi JSONL 就将 UI 标成一个新 Trial。

## 7. 验收证据等级

单次性能测量带噪声，不能直接驱动最佳候选、停滞判断和 Goal 完成。既有 Cummin 档案就有这样的例子：
- 单次最高的 76.07 没有晋升；
- 已晋升版本对父版本的配对比较为 −0.034；
- 同一版本本地 75.85、官网 74.99。

### 7.1 task 必须声明

- 测量口径 `measurement_profile`：设备与环境、工具链版本、核数上限、预热与重复次数、计时方法。口径带版本号，回执记录所用版本。
- 正确性标准：完整用例集、定向检查（边界、非对齐、NaN/Inf 等，按算子适用）、容差。
- 性能目标及其证据等级：本地目标与平台目标分开写；判定 Goal 完成用哪一级，写在 task 里。
- 噪声阈值：默认由 Goal 开始时对基线的重复测量估计，测量口径变化后重新估计。
- agent 可改动的源码范围。其余文件，包括评测脚本、用例、参考实现和 task spec，在执行边界内只读。

### 7.2 等级

| 等级 | 内容 | 记录为 |
| --- | --- | --- |
| E0 | 编译通过 | 可编译候选 |
| E1 | 完整用例集与定向检查全部通过 | 正确候选 |
| E2 | E1 ＋ 正式口径下的完整性能单次测量 | 单次观测；只展示，不更新最佳候选 |
| E3 | E2 ＋ 与父版本同设备的配对比较（ABBA 或交替重复，首个版本以基线为父），差值超过噪声阈值 | 已验收候选，可更新最佳 |
| E4 | 平台或官方评测回执 | 平台成绩；与本地成绩分开记录，不互相换算 |

配对比较计入评测预算。

### 7.3 各决策使用的等级

- 更新"最佳已验收候选"：需要 E3，且差值为正、超过阈值。配对结果非正时不晋升，即使单次分数更高。
- 自动 fork 与停滞判断：基于 E3 结果，或 E2 在噪声带内的持续窗口。缺测量或测量失败时先核查评测，不判停滞。
- Goal 完成：达到 task 声明的等级和目标，只认当前 task version 的回执。
- 交接材料和界面里，单次最高、已验收最佳、平台成绩分三栏展示。

### 7.4 回执内容与评测完整性

每份回执记录：
- `source_sha256` 和二进制哈希；
- `evaluator_sha256`，即评测脚本、用例和 task spec 的合并摘要；
- `task_version` 和 `measurement_profile_version`；
- 执行环境；
- 逐用例原始数据的引用。

评测在冻结快照上运行。快照只取 task 声明的可改源码；评测脚本、用例和参考实现从受控的只读位置注入，不从 agent 工作区读取。`evaluator_sha256` 与 task version 对不上的回执作废，不参与任何判定。

## 8. 流式显示与业务工具

模型事件贯通 `provider → KernelBridge → Pi → InteractiveMode`。增量只用于展示；最终消息、工具调用参数、usage 和 stop reason 持久化后才进入下一次执行准入。工具参数未完成校验前不执行。

控制通道与事件流、效果请求分开。当前串行效果队列继续保护执行顺序；取消、输入接收、状态查询和 UI 重连不等待一个长 NPU 工具请求返回。NPU 长评测使用提交回执/job handle 与查询接口；轮询属于宿主后台任务。

原生 `read/write/edit/bash/grep/find/ls` 保留名称、schema 和展示；底层 operations 统一进入执行器。Knowledge、IR、profiler、NPU 编译/评测和凭据读取通过 profile 注册。IR/profiler 输出展示源码哈希、设备、工具链和 measurement profile，原始证据通过引用读取。

Linux 使用既有 bubblewrap 路径，macOS 使用 Seatbelt 路径；NPU 在声明的远端执行环境运行。TTY 子进程只解决交互和运行时分离，文件/命令/网络隔离由执行器完成。评测脚本、用例、参考实现和 task spec 在两种沙箱中都以只读方式提供（见 7.4）。

## 9. 文件与模块安排

以下新增路径是实现分工，最终按现有模块边界合并，避免创建第二套产品 API。

| 路径 | 职责 |
| --- | --- |
| `source/contracts/execution/v1/application-host.schema.json` | 宿主请求、事件、输入回执、租约与接管合同 |
| `source/contracts/execution/v1/acceptance-evidence.schema.json` | 测量口径、证据等级、回执字段与评测摘要 |
| `source/apps/agent/src/application/host.mjs` | 常驻产品会话、单一调度入口 |
| `source/apps/agent/src/application/codegen.mjs`、`start-screen.mjs` | 当前项目 codegen、空闲启动、模型配置与恢复入口 |
| `source/apps/agent/src/application/client.mjs` | CLI/SDK/TUI 共用的本地客户端 |
| `source/apps/agent/src/application/input-queue.mjs` | 调用 Executor 持久接收与应用输入；不另建权威队列文件 |
| `source/apps/agent/src/application/worker-lease.mjs` | 交互/headless 租约、接管和恢复 |
| `source/apps/agent/src/harness/pi/runtime-factory.mjs` | 提取现有受控 Pi runtime 创建、恢复、重绑定 |
| `source/apps/agent/src/harness/pi/interactive-driver.mjs` | 原生 `InteractiveMode` 和终端生命周期 |
| `source/apps/agent/src/harness/pi/headless-driver.mjs` | 相同 runtime 的后台执行 |
| `source/apps/agent/src/harness/pi/host-actions.mjs` | 命令闸门与分阶段白名单、输入回执、退出拦截、user_bash 与宿主协议适配 |
| `source/apps/agent/src/harness/pi/product-ui.mjs` | Goal 状态、任务/候选/job 展示 |
| `source/apps/agent/patches/pi-coding-agent-1.5.0-host-actions.patch` | 在 `onSubmit` 入口插入命令闸门调用的显式补丁，附兼容性测试 |
| `source/apps/agent/src/sdk/application-session.mjs`、`session.ts`、`goal.ts` | 持久宿主客户端与旧 manifest 式公开 API 兼容层 |
| `source/apps/agent/src/loopx/*` | 保留策略与业务适配，归并重复生命周期驱动 |
| `source/packages/executor/.../state/`、`operations/` | 输入/Goal/租约事件、原子 admission、幂等恢复 |
| `validation/tui/` | 本交付源码可直接运行的协议、故障注入与 PTY 验收入口 |

首发 CLI 保留 `kernelagent` 命令名与原非交互子命令。现有 delivery 缺少上游完整 tests 目录，因此新增验收必须在交付仓库中自包含，并同步到开发仓库的正式测试目录。

## 10. 实现顺序与阶段出口

### P0：契约与原生 TUI 最薄链路

确定宿主 schema、输入 ID、账本事件、租约契约、证据等级与安全边界；用锁定 Pi 做命令闸门补丁。提取最小 runtime 工厂，通过原生 TUI 执行固定模型返回的 `read → edit → evaluate`，并用 SDK 跑相同脚本。此阶段宿主随前台启动器存活，不做后台接管。

出口：
- 两入口经过相同工具执行器，账本因果关系一致；
- 键入文本在闸门取得回执；同 ID 重试不产生第二次效果；宿主不可用时输入被明确拒绝；
- `!`/`!!` 经 `user_bash` 进入执行器；
- 白名单只开放普通输入、`!`/`!!` 和退出，其余命令全部被拒绝；
- `/quit`、Ctrl-D 和信号退出都被宿主记录：有未结算效果时拒绝退出，或停止调度后干净关闭；worker 无清理退出（含 129 紧急退出）时，宿主能检测到并收回执行授权；
- 三个回执窗口的故障测试通过：回执已落盘但 Pi 未保存、远端提交成功但回复丢失、Pi JSONL 末尾被截断。

先用固定响应验证，再跑一个真实 Vector Trial。

### P1：共享 runtime 与原生交互

完成 runtime 工厂、interactive/headless driver、CLI 入口与状态 UI；接通真实 token/tool-output 流；SDK 迁移到相同接口。每次 session 替换重新绑定订阅、资源和执行准入。白名单开放 5.2 中标为 P1 的命令：`/goal` 展示、`/compact`、`/tree` 只读浏览、模型与配置、展示类命令和凭据跳转。

出口：
- 生成、工具调用、修复、长输出、中断、resize、中文和多行输入都在真实 PTY 中通过；
- 模型调用（含压缩）和用户 bash 都有账本记录；
- `/model`、`/scoped-models` 选不出 profile 之外的模型；
- `/reload` 后冻结资源集合的哈希不变。

### P2：常驻宿主、持久输入与恢复

宿主与终端解耦；实现单 worker 租约、版本栅栏、事件游标、输入去重、本地连接安全及 NPU job 接管；实现 `on_detach`、`/detach`、`/resume`、`/new`、`/import`。SDK/TUI 都通过宿主提交，保留旧 SDK 调用形式作为兼容层。

出口：
- `on_detach=continue` 时，关闭 TUI 后活跃 Goal 继续；`pause` 时停止调度，可再 attach；
- SIGHUP 和终端断开后的接管与 `/quit` 一致；
- 再次 attach 恢复原产品会话与未结束 Trial；
- kill worker/host、丢失响应和重复 attach 均无重复提交或超额消费；
- 异 uid 连接、错误 token 和残留 socket 的测试通过。

### P3：LoopX Goal 与上下文完整闭环

Goal 版本与控制事件进账本，LoopX 状态改为投影；实现 `/goal` 控制命令、`awaiting_user`、语义 compaction、context reset、`/fork`、`/clone`、`/tree` 继续执行与自动 fork 策略。预算只从 KernelBridge 读取和结算；最佳候选与停滞判断按第 7 节的证据等级。

出口：
- Trial 内连续，跨 Trial 新上下文；
- 自动 fork 有策略证据与冷却/次数限制；
- 单次高分不更新最佳候选，配对非正不晋升；
- Esc 后 LoopX 不自动调度，直到用户输入或 resume；
- 用户新约束、有效回执、预算与未结束 job 在所有切换中保留。

### P4：设备与交付验收

Vector 和 Cube 分别走 TUI 与 SDK，覆盖编译、正确性、计时、配对比较、修复、验收和续跑；在真实设备上验证评测完整性（只读评测文件、回执中的评测摘要）；20-turn 压测检查长期上下文和事件稳定性。安装、恢复、README、示例 profile 和完整日志一起交付。

出口：下表规定的本地故障、真实终端和 NPU 验收通过后，更新 capability、依赖锁、源码清单、GitHub 和 ZIP。不会以单独运行一个 Pi 会话替代产品入口验收。

## 11. 验收矩阵

| 类别 | 必跑内容 | 通过条件 |
| --- | --- | --- |
| 原生界面 | 启动、补全、中文、多行粘贴、窄终端、resize、长输出、主题、正常/异常退出 | 无协议文本混入界面；退出恢复终端模式 |
| 全入口治理 | prompt、steer、follow-up、`!`、`!!`、原生命令、SDK 请求 | 相同 admission；发起者正确；密钥不进入子进程/session |
| 命令闸门 | 24 个内建命令、隐藏命令、未知命令、`/share`、`/bug`、`/debug`、模拟升级后新增的命令 | 只有当前阶段已适配的命令会执行；默认禁用项不外发，也不在运行目录外写文件；命令清单出现差异时测试失败 |
| 输入恢复 | 重复提交、接收后崩溃、应用后未确认、版本冲突、宿主不可用时提交、流式中与压缩中提交 | 同 ID 不重复进入对话；accepted/applied 状态可解释；宿主不可用时输入被拒绝且留在编辑器；三条提交路径都有回执，界面只显示一次 |
| 真实流 | token、工具输出、取消、断开订阅、慢消费者 | 真实增量可见；终态完整；慢 UI 不阻塞执行 |
| 权限隔离 | 只读参考、库外路径、符号链接、网络策略、用户 bash | 所有入口遵循同一执行边界 |
| 预算 | 模型、压缩、工具、评测、自动 Trial、崩溃后预留 | 不超额；恢复/fork 不重置总额；不确定消费保留预留 |
| 回执窗口 | 工具完成后 Pi 未保存、NPU 接受后回复丢失、Pi JSONL 末尾截断（模拟主机故障） | 返回原回执/查询原 job；设备提交计数不增加；截断时从账本恢复调用边界，不伪造 assistant 消息 |
| 运行权 | 双 attach、旧 worker 存活、租约过期、宿主重启 | 一个有效 writer；旧 epoch 的新执行被拒绝 |
| 本地连接 | 异 uid 连接、错误或过期 token、socket 权限被放宽、宿主崩溃后残留 socket 与锁 | 非本用户与无有效 token 的连接被拒；权限异常时宿主拒绝启动；残留项在确认无活动宿主后清理 |
| UI 脱离 | 生成时、评测时、空闲时关闭；SIGHUP；终端断开（129 紧急退出）；Esc 后关闭；后台再 attach | Goal 按 `on_detach` 继续或暂停；`awaiting_user` 不被 headless 解除；普通 session 等待；原 job 可追踪 |
| 上下文 | 连续压缩、超长工具输出、失败结果、待完成工具、新约束 | 原始 session 保留；事实不丢失；压缩不会重跑效果 |
| fork | 用户显式、agent 请求、LoopX 自动、历史候选撤销 | 父 entry/源码/Trial 对齐；无效历史凭据不被采纳 |
| Goal | pause/resume、Esc 后的 awaiting_user、目标版本变更、无进展、预算耗尽 | 控制事件可重建；完成只来自当前任务版本、达到 task 声明等级的有效验收 |
| 证据等级 | 单次高分、配对非正、差值在噪声内、测量口径变化、本地与平台成绩 | 单次结果不更新最佳候选；配对非正不晋升；口径变化后重估噪声；本地与平台成绩分开记录 |
| 评测完整性 | agent 写评测脚本/用例/task spec、回执的评测摘要与 task version 不符 | 写入被执行边界拒绝；不符的回执作废，不参与判定 |
| SDK/TUI 一致性 | 固定模型与工具脚本，经两个正式入口执行 | 归一化运行 ID/时间后，预算、调用顺序、因果与结论一致 |
| 真实 NPU | Vector、Cube 各含一次错误候选与修复，并完成正式评测 | 编译/正确性/性能回执绑定最终实际源码和环境 |
| 长会话 | Vector、Cube 各一次 20-turn 运行 | 无上下文重复执行、预算丢失、悬挂回合；记录性能曲线及结论 |
| 跨环境恢复 | Linux 本地、macOS 控制端加远端 NPU，换目录/无旧 HOME | 锁定依赖能安装；会话、源码和原 job 引用正确恢复 |

在线随机生成验收检查源码身份、回执和任务条件，不要求不同运行产生逐 token 相同的轨迹。性能提升由实际测量报告；正确运行与达到优化目标分别记录。

## 12. 日志与交付内容

```text
runs/<run-id>/
  task.json
  config.redacted.json
  events.jsonl
  state/events.jsonl
  state/pi-sessions/
  state/checkpoints/
  state/host/                 # 可重建索引和宿主诊断，权威事件仍在 Journal
  workspace/
  evaluations/
  loopx/                     # 带账本游标的 Goal 投影
  logs/host.jsonl
  logs/tui-events.jsonl
  logs/pty-transcript.txt     # 验收时生成的脱敏终端记录
  result.json
  acceptance.json
  manifest.json
```

权威只有两处：`state/events.jsonl`（执行账本）和 `state/pi-sessions/`（对话树）。`events.jsonl`、`loopx/` 和 `state/host/` 都能由这两处重建。`logs/` 下的文件只用于诊断，不参与任何判定或恢复。

`manifest.json` 至少记录：
- Pi 版本、命令闸门补丁摘要、白名单版本；
- 系统提示哈希、工具 schema 快照哈希、冻结资源集合（AGENTS 等）哈希；
- measurement profile 版本与评测摘要；
- 各文件之间的身份关联。

两次运行的行为不同时，靠这些字段区分差异来自模型还是 harness。

交付包括 TUI 与 SDK 使用说明、命令语义、环境 profile、Vector/Cube 示例、原生 session 和恢复脚本。生产会话不额外录制未脱敏的终端按键；验收 PTY 记录使用测试输入。文档中的功能状态从已通过的验收结果生成。

## 13. 版本与核对来源

首个实现基于已安装的 Pi `1.5.0`，避免把 TUI 改造和运行时升级混在一个验收单元。后续升级单独检查以下内容，并对系统提示与工具 schema 做快照比对：
- `InteractiveMode` 与 `AgentSessionRuntime`；
- `onSubmit` 中的字面命令匹配与 `BUILTIN_SLASH_COMMANDS`；
- 退出路径：`shutdown()`、信号处理、`emergencyTerminalExit()`；
- 扩展 hooks；
- `SessionManager` 的落盘方式与 session 格式。

- [Pi 1.5.0 SDK](https://github.com/earendil-works/pi/blob/v1.5.0/packages/coding-agent/docs/sdk.md)：会话、runtime 替换、输入时机和 settled 边界。
- [Pi 1.5.0 extensions](https://github.com/earendil-works/pi/blob/v1.5.0/packages/coding-agent/docs/extensions.md)：input、user_bash、会话生命周期和自定义 UI 扩展点。
- 本机 `InteractiveMode` 的构造参数为 `AgentSessionRuntime`，`shutdown()` 会 dispose runtime 并退出进程；按本文的 worker 接管契约处理 UI 关闭。
- 本机 `interactive-mode.js`：`onSubmit` 先匹配 24 个内建命令、`/debug` 与隐藏命令，再处理 `!`/`!!`，最后进入 input 回调；终端断开时 `emergencyTerminalExit()` 跳过扩展清理，以 129 退出；`/debug` 写入 `<agentDir>/pi-debug.log`。
- 本机 `session-manager.js`：以 `appendFileSync` 追加、不 fsync；首条 user/assistant 消息出现前不创建文件。`pi-agent-core` 的 agent loop 逐个 await 事件，assistant 消息在工具执行前落盘。

P0–P3 的本地实现与测试已落地。P4 的本地安装与回归按验收记录执行；Vector/Cube 真机、设备 20-turn、跨 Linux/macOS 恢复及发布继续保留为未验收项。既有版本的 NPU 成绩不能作为本次新宿主实现的通过凭据。
