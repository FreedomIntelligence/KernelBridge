<a id="finite-python-worker"></a>
# 有限 Python Worker

`kernelcat-worker` 0.8 显式组合 Executor、Knowledge、Ingestion，提供顺序、有界的 stdio
RPC。`kernelcat_worker.configuration` 解析配置，`kernelcat_worker.bindings` 是具体服务
组合根，`compose` 连接有限 transport。Node 产品应用拥有启动与 Pi 会话继续；自动多 Trial
必须由 LoopX 管理目标、Todo 和配额，Worker 不调度下一 assistant turn。

安装统一见[根目录 README](../../README.md#quick-start)。普通用户通过
[Agent CLI/SDK](../agent/README.md)运行任务；下列 Python 示例面向已准备部署的嵌入调用方。
Python >=3.11，默认安装不包含 Torch/CANN 或设备环境。

## 启动前提与输入

先按[CI 部署说明](../../docs/CI.md)独立准备冻结 wheel 部署，选择规范 `unpacked/` 为
`PYTHONPATH`，并将同一 `expected.json` 写入 Worker 配置的 `python_modules`。
固定 bootstrap 在导入 Worker 前检查包来源与字节。配置、Agent/controller 源码、状态、
显式 skills、合同、父本/case、evaluator 必须符合与可写候选 workspace 分离的规则。
拒绝的部署保持原样检查，不自动修路径、重建或清缓存。

以下是启动模板，所有绝对路径均须替换为真实部署输入；工作目录不参与包选择，建议从
仓库根目录调用。此命令随后等待客户端发送 RPC，不会自行运行 Agent：

```sh
KERNELCAT_ACTIVE_AGENT_ROOT=/absolute/apps/agent \
KERNELCAT_ACTIVE_NODE=/absolute/node \
KERNELCAT_MAX_FRAME_BYTES=1048576 \
/absolute/python -B -P /absolute/apps/agent/python/generated_startup.py \
  --expected-python-modules /absolute/deployment/expected.json \
  worker -- serve --config /absolute/config.yaml --task /absolute/task.json
```

Node launcher 自动提供实际 `KERNELCAT_ACTIVE_AGENT_ROOT`、`KERNELCAT_ACTIVE_NODE` 与
`KERNELCAT_MAX_FRAME_BYTES`；直接有限服务调用方须显式提供已准入身份。Worker 在打开
状态前校验其与 Config 相符，实际 frame limit 同时进入 transport 与 runtime binding，
因此 CLI 的 frame 设置一致覆盖文件值。配置中的 Agent root 须是独立部署的真实根，不能
指向候选 workspace；部署的其他保护边界仍须满足。

嵌入 Python 与 Ingestion 保留各自配置行为。Worker 不启动 Node 来探测版本，因此该
可执行版本观察可为 unknown；配置来源及依赖元数据仍被检查。解释器/部署是信任根，
`-B -P` 与 binding 检查不建立 OS 隔离。

## 托管 Run/Trial 服务

托管入口使用同一 bootstrap、身份变量及 Python binding，仅替换 `worker --` 后的参数：

```text
serve-runtime --config /absolute/config.yaml
serve-runtime --config /absolute/config.yaml --manifest /absolute/launch.json
```

以上是参数模板，不是独立 shell 命令。带 manifest 时还要求 Node 提供
`KERNELCAT_ACTIVE_RUN_MANIFEST_PATH` 与 `KERNELCAT_ACTIVE_RUN_MANIFEST_SHA256`，后者是
精确文件字节的 `sha256:<lowercase hex>`；实际文件和摘要在打开状态前验证。

无 manifest 只允许有限账户、状态、停止、Knowledge fence/sync 与 bootstrap complete。
有 manifest 时，`prepare_run` 保留真实 source/Program 字节和观察，`freeze_run` 在预期
account revision 接受闭合初始图。只有新提交的 freeze 或成功的显式当前 `replace_session`
绑定 execution Actor；已知 freeze replay 与状态查询不授予执行权限。

`prepare_trial`、`freeze_trial` 只选择连接所属原 Run 的下一 Trial。Executor 保留原账户、
合同、root、Run caps，fence 旧 assignment 并校验提议图；Worker 使用选定 owner 的 Task、
父本和独立 workspace/Session。绑定后的 stream 不切换 Actor，Node 负责新 Pi 构造与继续。
状态读取不改变当前 Trial，completion/finalization 显式指向原 Task/Trial。

Worker 通过类型化 port 向 Executor RuntimeOwner 提供真实 Journal、不可变 artifact 和
workspace materializer。Executor 保留 preparation、配置/provenance 和 Gateway/Journal
权威；Worker 选择具体 I/O。`versions` 同时 fingerprint Executor/Worker 源码；新实现不
改写历史 fingerprint 或 reservation。事务沿用本地 JSONL `O(E)` replay，artifact/workspace
I/O 和 provider 请求在锁外。

## RPC、上下文与输出

stdin/stdout 专用于 `kernelcat.pi-worker/v3` NDJSON，客户端只能发有限 ExecutionRPC。
每帧校验，操作顺序执行；`complete` 关闭已结算 session，提前 EOF 或非法输入明确失败。
teardown 将未决 delegated work 保留为 UNKNOWN 与 reservation，不重试中断效果。

Node wire 接受有限 JSON，整数范围为 `[-9007199254740991, 9007199254740991]`，更大的
精确身份用字符串；配置、Task、后续请求和观察使用同一生成规则，不舍入或重发。
嵌入 Python 值保留其已有 API。

TypeScript ContextCompiler 提交实际模型输入，Executor 检查必需事实、候选 suffix、当前
policy 与字节限制；预算在同一原 Journal model-intent 事务内重查，过期请求不调用 provider。
已知失败且仍有 route 时，仅刷新 `context_source` 预算，由 Node 重编译下个已授权 route；
UNKNOWN/transport failure 停止对账。已完成 proposal 回执仍可复用。

大结果使用 `kernelcat.rpc-payload/v1` 引用与顺序 `payload_read` 页；大请求使用
`payload_begin`、`payload_write`、一次 `payload_call`，upload 在调用原 owner 前消费。
引用限于本进程 session，校验精确字节数、SHA-256、连续 offset；传输中拒绝无关操作。
每页遵守 frame limit。临时 spool 在结束或失败时关闭；大 completion 等最后一页写出才退出。
这些字节传输不产生新 receipt 或 execution identity。

传输时间/临时存储随字节数线性增长。Node 先在内存重建完整 source，再选 context；聚合
默认 64 MiB，可通过产品 CLI `--max-payload-bytes` 调整。超限明确失败，不提供残缺 context，
也不改变模型 context budget。model upload 上限为 8 MiB + 4096 bytes；runtime 的
prepare/freeze/snapshot/adopt/stop/fence/sync（含 Trial）为 16 MiB + 4096；既有 native method
保持 4,198,400 bytes，均受各页 frame limit 约束。

生成后 Node 先 dispose Pi 再有限 snapshot/adopt，原 owner 最终报告 generation、settlement/
pending 与 evaluation qualification。只绑定有限 finalization 不开放普通 model/native 权限。
控制撤销后仅原 retained permit 可迟到 settle；新调用仍检查当前 Actor。若进程在已准入
模型 send 后丢失，finite complete 可报告 pending 与 unknown generation/evaluation，保留
reservation 和两个 lease，不造新结果；已知模型回复丢 ACK 则复用原 receipt。

<a id="knowledge-reads"></a>
## Knowledge 只读接口

`kernelcat_worker.knowledge.knowledge_tools(surface)` 提供四个顺序只读 Gateway 工具：

| 工具 | 输入与输出 |
| --- | --- |
| `knowledge_search` | owner 校验的结构化 KnowledgeQuery，返回投影与查询回执 |
| `knowledge_show` | 逻辑页或精确 revision，`max_chars` 限制可读文本 |
| `knowledge_source` | 已注册 source 身份与元数据 |
| `knowledge_read` | 由 evidence selector 与摘要校验限定的原文 |

可信 Worker 配置的 `knowledge_root` 指向已有资源根；相对路径按配置文件解析。规范根
必须与可写 state/workspace 分离，显式别名只要仍指向分离根即可。legacy 绑定配置中的
live application，managed v3 只绑定已安装 retained view；四工具进入 sealed MCP 目录。
模型参数不能换根、发布或指定任意 source 路径。实现 fingerprint 纳入 runtime binding。
每工具使用普通 one-credit quote 加既有 Gateway gate cost、guards 与原 Journal receipt；
重复 Action 复用结果，I/O 中断保留 UNKNOWN。`read_receipt` 仍是原 task-scoped reader。

managed v1/v2 拒绝 Knowledge 根。新 v3 可不启用 Knowledge；有 authority pin 的 v3 须配置
匹配根，并保持与 controller/skills/合同/parent/case/evaluator 分离。generic evaluator/
validator 仍须另行获得 target 资格。来源知识保留原 evidence maturity，不替代设备验证。

<a id="permanent-knowledge-consumer-fences"></a>
## Knowledge 永久 consumer fence 与恢复

新 `account-create --knowledge-authority SHA256` 选择 v2；无标志保留 v1；
`account-create --multi-trial` 选择 v3，可同时固定 Knowledge authority。既有账户不转换、
重置或再次充值。精确创建重放返回原账户，修改 body、复制 Journal authority、不支持的
header 或不兼容旧 managed 实现均在效果前拒绝。

`knowledge-fence --request ABS` 是独立 manifestless 管理连接，读取 no-follow/nonblocking
打开的普通文件（最多 16 MiB UTF-8），发送生成的 `KnowledgeFenceRequest`。配置选择
Journal/account，请求不能选择根或 Actor；它不是 model/native/delegate 目录工具。
原 Journal 校验身份、account、canonical Journal authority、固定 Knowledge authority 与
header，并持久保留精确 registration 和终态 Run/Trial fence，即使 preparation 尚未完成。
future Trial 的 fence 不停止当前其他 Trial；当前 Trial 的首次 fence 原子采用既有 stop、
未开始取消、generation/control 与双 lease 转换。后续独立撤回只追加自身 receipt。

fence 使用独立 `runtime_knowledge_control/v1` event/replay 域；公共 Stop/Replace 保持
原 `runtime_control` intent/body 索引，相同字符串不能混为同一控制。ACK 保留原物理
Journal sequence/event digest；丢 ACK 后同请求对账，返回原 receipt，不再追加或取消。
unknown v2 event 在新事务前拒绝。

未开始的原 zero-effect reservation 可释放；started/UNKNOWN 保留原身份/预留，原 native
permit 或 bound settlement 可按原验证迟到完成。pending 清零不恢复知识有效性。新 freeze、
context/model/native/delegated、materialization/adoption 仍由原 guard 拒绝，stop/dispose/report
可用；历史 receipt 不授予当前权限。`RuntimeState.knowledge_invalidated=true` 表示已提交
终态 fence，缺少该字段不证明知识可用。没有 TTL/reactivation 或 sequence-max 替代语义。

v3 Worker 在调用 Knowledge 前记录原 capture envelope，经 ProgramArtifacts 保存完整不可变
receipt，并在返回 preparation 前安装紧凑 binding。capture/fence callback 均在两侧事务外。
恢复载入含原 account/control revision 的完整 envelope，不能重造 capture ID。完整 receipt
是一个最多 16 MiB 的 context reference，普通 Program 仍为 8 MiB；当前撤回检查独立进行。

`knowledge_sync` 只接收 Run、Trial 与 1–100 的可选 limit；从原 Journal（包括未完成或已
替换 preparation）取原 capture body，把精确 pending fence 送回该 Journal，并返回 consumer
status。新效果前对账绑定 consumer，unavailable/pending/invalidated 均拒绝。父会话、native
leaf、只读 delegate 共享原 retained view 和 RuntimeStore Actor/account，不回退 live 知识。
原 `native_settle`/`plugin_settle` 仍可迟到结算，新调用不能借此绕过 guard。

JSONL replay 成本为 `O(E + retained graph/fence payload bytes)`，`F` 个 fence/registration
索引需 `O(F)` 空间；图仍最多 16384 对象。不宣称常数时间历史或多主机保证。事务内无
Knowledge call、callback、subprocess 或 artifact I/O。某 consumer 零 pending 不表示其他
consumer 已对账或全局 barrier 生效。

## 管理、采集与清理

`kernelcat-admin` 拥有管理检查与明确请求的历史 LoopX 操作；Python 产品 `run` 在读取配置
或打开状态前退出并提示使用 Node 入口。嵌入方可用有限
`ExecutionSession.run(retry_model=...)`，Node 产品不提供手动 model retry 参数。

只读管理使用同一 bootstrap 的 `worker-admin` 模式，以下仍是绝对路径模板：

```sh
/absolute/python -B -P /absolute/apps/agent/python/generated_startup.py \
  --expected-python-modules /absolute/deployment/expected.json \
  worker-admin -- versions --journal /absolute/state/events.jsonl \
  --startup-report /absolute/reports/current-reader.json
```

可选 startup report 只创建新文件并标识当前 reader，stdout 保持原 JSON；历史 Journal 与
fingerprint 不变，不据当前实现认证旧执行。嵌入管理方显式传 `expected_modules`，直接 CLI
也可在 subcommand 后传 `--expected-python-modules`；缺失/冲突在构造资源前失败。

`worker -- ingest ...` 提供显式来源初始化/采集，`worker -- operator-history ...` 提供
`collect`/`admit`/`read` 的有界历史导入接口；具体参数以各命令 `--help` 为准。二者仍经
原 Journal/Gateway 和 binding 检查，不隐式安装或发起网络采集。Worker 将具体 Executor、
Knowledge 与 composition source 摘要和 Ingestion fingerprint 一并绑定，不能复用旧来源身份。

历史 `loopx-cycle` 绑定明确 plan 并启动 Node `src/loopx/host-cli.ts`，随后使用固定
`loopx-host` Python bootstrap。expected file/launcher 须在候选目录外；child 保留已准入
import 优先级。原 plan 身份含 bootstrap/Node entry 字节。`loopx_open` 先于普通 RPC，Pi
完成后禁止新效果但保留锁，Node dispose 后 `loopx_finish` 按原 completion 写回；缓存结果
不启 Pi。writeback-only 检查原 task/config/settled segment 及 module observation，再通过
原 Journal 准入。未观察的历史 campaign 使用其固定 checkout，不改历史身份。

正常客户端完成后关闭 stdin 并等待 Worker 退出；中断只清理本地 transport/spool，原
UNKNOWN、lease、reservation 和历史证据仍用于对账。不能因进程退出判定远端已取消，
不能删除状态来强行继续。

## 离线验证

从仓库根目录运行 `python scripts/ci.py --suite worker`，由 dispatcher 保留组件 cwd，使用
明确冻结部署执行测试，并对 source 做 lint/types。`KERNELCAT_TEST_PYTHON_BINDING` 由
它提供，不从环境 import 猜 expected 实现。完整入口与依赖见 [CI](../../docs/CI.md)。
临时 loopback fixture 只验证软件协议，不证明模型质量、设备性能、生产迁移或全局撤回 barrier。
