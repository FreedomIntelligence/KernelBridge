<a id="kernelcat-knowledge"></a>
# Knowledge：来源、证据与知识检索

`kernelcat-knowledge` 提供 `kernelcat_knowledge`，管理不可变来源字节、来源/证据目录、校验、检索和经审阅的发布。Python、`kcwiki` 和 MCP 使用同一有限应用 `api.application.WikiApplication`。导入包或传输模块不会打开数据库、定位语料或启动服务。

安装统一见[根 README](../../README.md)，可选 MCP 依赖须显式选择；这里不重复维护安装命令。与 KernelCat、LoopX 的接入关系及日常检索流程见 [KernelBridge](../../docs/customer/KERNELBRIDGE_README.md)。Knowledge 不自行调度 Agent 或授予实验预算。

## 使用前准备

资源与安装代码分离。显式传入资源根，或为 CLI/MCP 设置 `KERNELCAT_WIKI_ROOT`；既有仓库资源根为 `wiki/engine`，其中 `config.yaml`、`raw`、`data`、`artifacts`、`wiki` 保留原身份。代码不从安装目录或调用方 cwd 推断语料。

实际启动要求（控制台示例显式设置 `PYTHONDONTWRITEBYTECODE=1`，防止在准入包内留下缓存）：

- 根安装流程选择的不可变普通 Python 源文件部署；不允许产品字节码缓存、符号链接包或自定义 loader。
- 独立审阅的 `ExpectedPythonModules`，精确包含 `kernelcat_knowledge` 一个模块。它不同于 Worker 的四包 binding，权威格式见 [Python 模块绑定合同](../../contracts/execution/v1/python-modules.md)。
- 独立 CLI/MCP 必须指定新的绝对 `--startup-report` 文件名。父目录先创建，位于所有准入包树之外；报告 create-only，不覆盖、不自动建父目录、不写 stdout。缺少目录、路径冲突或权限失败均在业务 store/server 构造前停止。`--help` 保持无效果。

以下命令可从仓库根目录运行，使用所选环境的入口；所有 `/absolute/...` 都是需替换的路径模板。计划、请求等相对文件路径由调用方 cwd 决定，推荐使用绝对路径。

```sh
PYTHONDONTWRITEBYTECODE=1 kcwiki --repo-root /absolute/wiki/engine \
  --expected-python-modules /absolute/config/knowledge-modules.json \
  --startup-report /absolute/reports/knowledge-search-startup.json \
  search --request /absolute/requests/query.json
```

`query.json` 示例（替换为实际语料中的算子身份）：

```json
{
  "schema_version": "kernelcat.knowledge-query/v1",
  "operator_id": "vector_add",
  "phase": "performance",
  "bottleneck": "memory traffic",
  "decision_question": "Can redundant input reads be removed?",
  "limit": 3,
  "token_budget": 1200
}
```

`operator_id` 与 `mechanism_family` 二选一；`limit` 为 1–10，`token_budget` 为 256–1200。输出为 `kernelcat.knowledge-search-result/v2` JSON，包含 `projection`、`receipt` 和完整响应的 token 估计；检索到的内容取决于实际资源。沿精确修订和 evidence 引用读取原始依据，不能把命中当作候选正确性或设备性能证明。

MCP 需要根安装说明中显式选择的 `mcp` extra（FastMCP），仅启动服务时加载 SDK：

```sh
PYTHONDONTWRITEBYTECODE=1 KERNELCAT_WIKI_ROOT=/absolute/wiki/engine kcwiki-mcp \
  --expected-python-modules /absolute/config/knowledge-modules.json \
  --startup-report /absolute/reports/knowledge-mcp-startup.json \
  --host 127.0.0.1 --port 3000
```

该命令启动 streamable HTTP 服务，详细工具和兼容边界见 [MCP 说明](src/kernelcat_knowledge/api/mcp/README.md)。启动不安装包。结构合同检查另有真实入口：

```sh
PYTHONDONTWRITEBYTECODE=1 kcwiki-validate-contracts --repo-root /absolute/wiki/engine
```

它输出错误及 `v1_contract_errors=N`，无错误退出 0，否则退出 1；可重复 `--object` 指定对象，非绝对对象路径相对 `--repo-root` 解析。结构检查不等于来源资格、发布审批或执行验证。

## Python API

以下是嵌入片段，`reviewed_binding` 是调用方提前审阅准备的 Knowledge-only binding，路径须替换为已有资源，不是完整独立程序：

```python
from pathlib import Path
from kernelcat_knowledge import build_application, open_evidence_store

application = build_application(
    Path("/absolute/wiki/engine"), expected_modules=reviewed_binding,
)
report = application.startup_report
application.export_startup_report(Path("/absolute/reports/python-startup.json"))
store = open_evidence_store(Path("/absolute/external-corpus"))
```

`open_evidence_store` 只打开已有 corpus；创建新外部根须显式 `initialize_corpus`。`CorpusError` 是公开错误边界。Capture 保留首次来源观察，目录写入方共享持久 lock inode，不可变冲突明确失败。导入来源不会激活知识卡，也不建立目标验证结果。

<a id="fact-eligibility"></a>
## 事实可用资格

默认检索、Python `card_validate`、CLI `card validate` 与 MCP 检索使用同一 `OfficialFact`/`PlatformFact` 资格规则：事实必须 active、带有明确 scope 的 claims，成熟度为 `source_derived` 或 `api_valid`。每个卡级及 claim 级 EvidenceRef 都须解析为已注册 active 来源，通过完整来源摘要与精确 selector 校验，含准入的 `integrity=verified`。每项 claim 的 authority scope 须由其每个引用来源覆盖，`unknown` 不能覆盖。两个事实类型目前只支持 `official_source`，不能从卡片类型推导社区或推断来源的权威性。

这是已注册 authority 声明下的来源资格，不认证发布者，也不判断自然语言 claim 是否由所选文本推出。`api_valid` 不是编译观察。V1 卡片没有这里支持的 compile/run/benchmark/formal/landed receipt 绑定；这些成熟度、draft/retracted、缺少 scoped claims 均拒绝。旧 `verification` 字典不能补造回执。规则不改已存卡片、revision、source ID 或 evidence ID。

失败事实在排名前排除出 `must_obey`，检索返回有界不确定性（失败计数和首个原因）；`card validate` 返回含精确 revision/首个失败义务的 `TRUST_GATE_FAILED`。低层 `validate_object`、合同 validator、索引重建只检查结构；结构 `valid` 或发布成功都不代表事实资格。非事实检索、scope/phase 过滤、精确修订审计读和发布规则不变。

保留视图对原 manifest 与封存字节应用相同规则，不退回当前来源；capture 不升级成熟度或权威，后续读仍检查当前撤回。每次只检查 scope 匹配卡片，无全局缓存或历史扫描。每事实 R 个不同引用的成本为 O(R 次定向目录查找 + 所引来源校验成本总和)，跨引用/卡片可重复验证来源，每次调用重读当前字节。原 verifier 限制文件 16 MiB、目录 100,000 成员/256 MiB，artifact 读保留 resolver 限额；内存为 scoped 卡集、单卡引用/scope 集合及当前来源/range 数据，响应仍受完整结果预算约束。

<a id="neutral-resource-manifests"></a>
## 中立资源 Manifest

`ResourceCatalog(root, manifest)` 按 ID、版本、预期 SHA-256 解析资源，不创建 TaskContract，也不从 manifest 加载代码。以下为身份格式模板；全零 digest 必须替换为已审阅的真实摘要：

```python
from pathlib import Path
from kernelcat_knowledge import ResourceCatalog

catalog = ResourceCatalog(Path("/absolute/task-inputs"), {
    "resources": [{
        "resource_id": "task:user-fusion", "version": "1",
        "digest": "sha256:" + "0" * 64,
        "selector": {"path": "taskpacks/user-fusion.json"},
    }],
})
reference = catalog.resolve("task:user-fusion", "1", "sha256:" + "0" * 64)
```

最多 10,000 个唯一 ID/version；selector 只支持规范相对路径。`catalog.read(...)` 有界读取、拒绝符号链接组件和非普通文件、比较完整文件摘要。身份缺失、版本/摘要或字节变化均明确失败。资源仍是受保护输入，解释和评估由各自管理方负责。

<a id="retained-consumer-views-and-retraction"></a>
## 保留视图与撤回

有限 API 详见 [Knowledge runtime 协议](../../docs/protocols/knowledge-runtime.md)，请求/回执使用生成的 `knowledge_runtime` 类型。Runtime validator 使用生成 schema，卡片/来源/证据校验仍由原管理方负责。`control_status()` 是只读，包括初始化前；显式 capture/retraction 可在 `artifacts/kcwiki/retained` 写元数据与不可变 blob。Control header 绑定规范资源根，拒绝在另一个规范根打开副本。

以下请求是协议形状示例；consumer 与 Journal 身份必须来自可信装配，不能把示例字符串当作真实执行权限：

```python
status = application.control_status()
receipt = application.capture_consumer({
    "schema_version": "kernelcat.knowledge-capture-request/v1",
    "intent_id": "capture:trial-1",
    "expected_control_revision": status["control_revision"],
    "consumer": {
        "consumer_id": "consumer:trial-1",
        "journal_authority_digest": "sha256:" + "a" * 64,
        "account_id": "account-1", "run_id": "run-1", "trial_id": "trial-1",
        "required_journal_header": "runtime_account_initialized/v2",
    },
})
view = application.open_view(receipt["view"])
```

`view` 提供 `search`、`show`、`source_show` 与仅 evidence ID 的 `raw_read`。Capture 先记录意图和不可变 consumer，再持久准备；在原持久 owner lock 下按物理目录 device/inode 排序选择新元数据，再取 retained control lock。所有参与卡片身份扫描的目录都在锁集内，文件输入使用发布父目录，交换 card/source 配置不能逆转顺序。卡片发布、两个 source repository、公共 corpus capture 和 evidence 注册使用同一组 owner lock；持 control 时不会再取更早的锁。大来源读取/hash/blob 安装在元数据事务外。持久 selection 重试不重选；seal 复查 deny，seal 前赢得竞态的撤回永久使 capture 无效；中断可留下未引用 staged blob。

选择涵盖原文件系统 loader 的 static/dynamic index、`wiki/cards` 与 rule index。新 selection 对 active V1 卡片应用 pending/effective deny：排除 card/source/evidence 精确命中，再按逻辑或 revision ID 排除反向 `depends_on` 闭包，只保留存活卡片及其精确证据、来源记录和必需字节。历史状态/ID/payload 不改。持久 selection 永不重算；后来胜出的撤回使其永久失效。旧 deny 后可新建干净视图，不扩大旧固定 barrier；新撤回仍保守包含 selection 未知的 pending capture。

只有 legacy 卡片、未解析依赖、不支持格式或注入/custom loader 会拒绝整个 capture。Whole file、artifact/CAS、目录 manifest 和精确行选择使用原全来源/selector verifier。历史 revision ID 不变；完整规范 payload digest 区分 whitelist 排除字段。首次发布者串行化；重复发布比较完整已接受 payload，不覆盖文件或静默替代卡片。

保留检索复用 scope/phase/ranking/projection 与确定性 fallback，不使用可变 FTS。精确 ID 和查询 evidence 须是视图成员；读校验必需保留字节，来源读保持全来源校验和只缩小范围的分页，不从当前根补数据。Human page rendering 因外部渲染输入未保留而不可用。

打开及每次读视图都检查永久 invalidation。Consumer ID 和完整 Journal/account/Run/Trial 身份各自唯一，换 consumer/intent 不能替换同一 Trial 的视图。已 seal 重复 capture 即使后来 invalidated 也只返回历史回执，不重新注册或激活；未 seal 且已失效则拒绝。回执不授予当前效果权限。

`begin_retraction(request)` 对 deny revision 做 CAS 并固定 consumer 集合，含 selection 未知的 pending capture，立即关闭匹配视图。`retraction_status(intent_id)` 区分 `pending` 与 `effective`；后者只表示固定注册集合全部 ACK，空集自然成立，不覆盖未注册旧进程。不可用、TTL 或进程退出不移除成员。独立撤回保持独立 invalidation/精确 ACK，不受投递顺序影响；没有恢复 API 或按最大序号采用规则。

`pending_fences(intent_id, cursor=None, limit=100)` 按 UTF-8 consumer ID 分页。`reconcile_retraction(intent_id, deliver_fence, limit=100)` 在所有 owner lock 外投递一页，校验并持久化 ACK。只有注入的可信 callback 可提供回执，须精确匹配 fence/request、registration、要求的 v2 Journal header、authority 和 account。Callback/ACK 丢失保留原请求和固定成员，错误含原 intent 与当前状态；这些 API 不发现 consumer、不授予效果、不改 lease/预留/记账。

可信嵌入还可用 `consumer_status(original_capture_request)` 和 `reconcile_consumer(original_capture_request, deliver_fence, limit=100)`。两者匹配完整原准入 body 和原 CAS；后来的 revision 不改变查找。未知 capture 返回 `NOT_FOUND`，body 改动冲突，authority 副本拒绝，不会重 capture。未 seal 注册在 capture 确认丢失后仍可查询。

返回的 `KnowledgeConsumerStatus` 包含原注册、当前 control revision、严格布尔终态 invalidation 与该 consumer 待 ACK 数。仅查 control 元数据，不可用 live source 或损坏 retained payload 不改变状态；payload 读单独报告失败。Pending=0 不恢复已失效注册，也不使其他 consumer 的全局 barrier 生效。

Consumer 对账按撤回 intent UTF-8 字节选至多 `limit` 个原请求，严格整数范围 1–100；较早不可用 consumer 不阻塞选择。Callback 在元数据事务及 card/source/evidence 锁外执行，使用相同精确 ACK 校验与不可变存储。失败抛带 cause/当前类型化状态的 `FENCE_DELIVERY_FAILED`；未知 ACK、持久化回滚仍保留 pending。ACK 已提交但回复丢失时，重试观察原记录而不再投递。重复/逆序投递不推进 control revision。这两个方法不新增 CLI/MCP/model discovery。

CLI 管理子命令须接在上文完整 `kcwiki --repo-root ... --expected-python-modules ... --startup-report ...` 前缀后，**每次使用新的报告文件名**。以下仅列子命令模板，不是独立 shell 命令：

| 子命令模板 | 输入/结果 |
| --- | --- |
| `control status` | 读取 control 状态 |
| `capture --request /absolute/capture.json` | 准入 capture，返回原回执 |
| `view --reference /absolute/view.json search --request /absolute/query.json` | 查询保留视图 |
| `view --reference /absolute/view.json show CARD_ID --view audit` | 查看精确成员 |
| `view --reference /absolute/view.json source SOURCE_ID` | 查看成员来源 |
| `view --reference /absolute/view.json read --evidence EVIDENCE_ID` | 读取证据原文 |
| `retraction begin --request /absolute/retraction.json` | 条件撤回 |
| `retraction status INTENT_ID` | 查看固定 barrier |
| `retraction pending INTENT_ID --limit 100` | 分页待投递 fence |
| `retraction reconcile INTENT_ID --limit 100` | 仅有可信注入 callback 时可投递 |

Capture/retraction/reference JSON 上限 32 KiB，JSON 递归失败在 capture 准入前返回 `INVALID_JSON`/退出 2。普通 CLI 没有 Journal delivery callback，`reconcile` 返回 `TRUSTED_FENCE_DELIVERY_REQUIRED` 及当前状态；可信调用方可注入 `api.cli.run`，没有 ACK-file/provider-loader 选项。MCP 不增加发布/控制权限。旧 root 读及 `snapshot create` 审计 manifest 保持独立。

托管 Worker 的 v1/v2 账户仍拒绝 Knowledge；新 v3 `multi_trial` 账户可由原 Worker/Executor 管理方固定 authority、capture/install 并绑定只读工具，详见[架构](../../docs/ARCHITECTURE.md)与 [Executor](../executor/README.md)。单独 Knowledge fixture 不代表该产品接入已经验收。

Capture 限额：4096 active 卡、16384 dependency member、16384 artifact；每 metadata/artifact 16 MiB，规范记录+artifact 总计 64 MiB。元数据扫描至多 16384 文件/行、64 MiB 输入，目录枚举 16384 entry；source selector/reference 至多 16384。任何超限拒绝整个视图。查询/投影/raw 输出保持原限额。

### 复杂度与存储边界

记 N 为扫描 metadata 行，C 为选中卡，M 为依赖成员，A 为 artifact，B 为保留字节，D 为不同 deny target，T 为注册 capture，L 为卡引用/依赖，F 为固定 barrier 大小，R 为全部 fence 行，P 为单 consumer pending 行，I 为可选索引中全部 pending 行，K≤100 为所选页；H 为原来源校验处理字节，可因重复全来源验证而超过 B。

| 操作 | 实际成本和工作内存 |
| --- | --- |
| 新 capture | 扫描/hash O(输入字节+N+M log M)，deny O(M log D)，反向闭包/过滤 O(C+M+L+选中记录字节)，来源验证 O(H)，artifact 排序 O(A log A)；保留解码 metadata/descriptor、反向边和一个有界 artifact |
| 已 seal 重复 capture | 索引 intent 查找及 O(回执字节)反序列化/校验，不重扫来源/root |
| 打开 view | O(B+M+A) hash/建索引；descriptor/manifest 加一个有界 blob |
| Search | O(所选卡字节+C log C)加原投影/token 处理与索引 current-view 检查；O(所选卡 metadata)内存 |
| Show/source/evidence | 索引 current-view 与内存成员查找，加所选记录字节及所需 O(H)全来源校验，保留原行/字节界限 |
| 新 retraction | 精确成员查找或有界 owner scan，一次 O(T) consumer scan、view 查找、O(F) fence 创建及 SQLite 更新；逐条处理 |
| Status/pending/reconcile | Status 计数 O(F)，keyset 读 cursor 后 pending/ACK 直到满页；每次至多 100 投递/ACK，按需读当前状态，页内存 O(100) |
| 有新索引的 consumer status | Capture 索引/请求/注册字节加 O(log(I+1)+P) pending range；计数只用 scalar，不读 payload |
| 有新索引的 consumer reconcile | O(log(I+1)+K) entry 及 fence 字节；至多 K callback/ACK，原主键查找/更新/部分索引删除后读状态，O(K 请求)页内存 |
| 旧无索引 consumer 元数据 | 每次 count/selection 可扫 O(R) fence，加 SQLite 表/索引及排序；只解码 K 条，投递/ACK 上限不变 |

新 control DB 对未 ACK fence 建部分 `(consumer, intent COLLATE BINARY)` 索引。旧 v1 DB 不因打开、status、对账或 ACK 增加索引、迁移 schema/历史行，两者 metadata format 都为 1。SQLite query/cache 空间另计，fixture 计时不是存储性能结论。历史增长仍增加索引访问成本，没有总读常数时间、索引归档、有界总历史或跨 store exactly-once 承诺。原 evidence/catalogue 仍由原管理方维护，retained store 只保存固定读选择与当前 consumer/deny 元数据。

<a id="component-checks"></a>
## 组件检查

通过[根安装及检查流程](../../README.md)和 [CI](../../docs/CI.md)使用冻结部署及 dispatcher 指定的组件 cwd/导入根；不要用临时源码导入命令替代组件 gate。测试覆盖原文件系统完整性、重复/冲突、selector、发布、跨入口锁、中立 manifest 和冷导入，显式提供资源根。Fixture 证明存储/应用合同，不证明检索质量、模型质量或 kernel 性能。

<a id="responsibility-layout"></a>
## 代码职责

| 模块 | 职责 |
| --- | --- |
| `resources` | 显式资源根/配置和 ID/manifest 解析 |
| `sources` | 不可变来源注册、查找、验证 |
| `evidence` | Bundle 准入、selector、来源完整性、有界读取和来源关系 |
| `cards` | Active revision 规范化、校验、diff 和审阅发布 |
| `query` | Scope/phase 过滤、词法检索、有界 Context 投影 |
| `trajectories` | 幂等注册和 outbox |
| `storage` | 原文件系统/CAS 目录、持久锁、可重建索引 |
| `api` | Python facade、显式装配、CLI/MCP 适配 |

旧 `application`、`adapters` 目录已退役，调用方使用归属模块，无导入别名或第二份可写目录。Python facade 委托同一规则；配置解析在 `resources/configuration.py`，storage 不导入应用装配根。

| 原模块 | 当前模块 |
| --- | --- |
| `bootstrap` | `api.bootstrap` 装配；`resources.configuration` 解析路径 |
| `application.source` | `sources.registration` |
| `application.evidence_bundle` | `evidence.bundles` |
| `application.read/search/legacy_search` | `query.read/search/legacy` |
| `application.trajectory` | `trajectories.registration` |
| `application.validation` | `cards.validation` |
| `application.wiki` | `api.application`，操作归属各领域与 storage |
| `application.errors` | `ports.errors` |
| `adapters.cli` | `api.cli` |
| `adapters.catalog_io/corpus/filesystem` | `storage.catalog_io/corpus/filesystem` |
| `adapters.filesystem_evidence/filesystem_sources/filesystem_trajectory` | `storage.artifacts/sources/trajectories` |
| `adapters.legacy_cards/legacy_index_loader/page_reader/raw_reader/sqlite_read_model` | `storage.legacy_cards/legacy_index/pages/raw/index` |
| `adapters.source_content` | `evidence.source_content` |
| `contracts.projection` | `query.projection` |
| `trust.models/validator` | `cards.trust_models/trust`；CLI 在 `api.tools.validate_trust` |
| `api.mcp.query_contract/trajectory_store` | `query.filters/storage.legacy_trajectory` |

Legacy reader 仍在 storage 边界服务既有 MCP/stage-2 接口。历史资源字节、EvidenceRef、selector、revision ID、锁身份与发布规则不变；打开 corpus 仍不初始化，来源/证据目录继续使用原持久锁和不可变冲突。

<a id="structured-query-provenance"></a>
## 结构化查询来源记录

Python `search`、CLI `search`、MCP `wiki_search_v1` 返回 `kernelcat.knowledge-search-result/v2`：原 V1 projection、V2 receipt 和完整 envelope 的 token 估计。读取 `result["projection"]` 获取投影字段。固定 envelope 装不下时抛原 owner 错误；请求上限仍 1200 token，512-token 请求可能比旧投影响应保留更少卡片。Receipt 按投影顺序列出实际保留卡片、完整规范 query/projection hash、实测查询延迟及 null adoption。Gateway 重放仍用原回执。

每次装配固定一份有效资源配置；相对路径相对规范 corpus，等价默认值/路径写法/YAML 注释不改 digest，配置修改须显式 rebuild 才生效。Python、CLI、MCP、发布、capture 共用所选路径与原锁；禁用可选索引不自动打开。外部 live reader 可用，但发布/capture 在新意图前拒绝 outside-root 资源。新卡发布在 owner lock 内比较当前 card 输入/输出管理方与 frozen binding，旧绑定返回 `RESOURCE_CONFIGURATION_CONFLICT`，不安装卡片或采用新路径。更换 owner 前须隔离 writer 并对账；编辑 YAML 不是迁移。历史 capture 重放/精确成员撤回保持原权限；任意注入 repository 的 corpus/config identity 为 unknown，不能 capture。

Schema/policy identity 在装配时从已加载 generated schema、query、validation、eligibility、normalization、storage、deny-policy 实现与语义常量计算一次。Code-object 序列化排除文件名/行号，包含 Python major/minor；规范遍历函数 body/default/closure，含 contextmanager generator，最多 500,000 value/深度 64，循环或不支持状态明确失败。生成方法引用的 class 关联单独 hash 的已加载 class body，并显式支持标准库 object/FrozenInstanceError。查询不重读后来修改的源码、不调用 Git；此身份是执行策略分组，不是跨语言 build hash 或 corpus 内容 hash。

新 selection 在原 commit 点保存有界规范 resource descriptor，V2 manifest 将其纳入 seal digest，member-byte counter 含义不变。旧 V1 manifest/pending selection 保持可读，capture config 为 unknown；重试保留原 selection/receipt。Deny 同时影响 live 检索、依赖卡和 retained 读。Live search 在 loader 原锁内复制一个 index generation/revision，按同一 per-card validation/isolation 检查，再对这些精确卡查 deny；并发发布不能进入已复制快照。原规范 record digest 仍是 deny 身份。

仅匹配卡及其传递 parent 闭包解析依赖 metadata，无关坏证据保持隔离，必需 metadata 错误明确失败，跨 scope deny 仍传播。Catalog 解析在数据库事务外，随后按原分页精确查 deny。Live file/external selector 不取发布目录锁。复制 O(所选 entry 字节)，依赖传播 O(卡+依赖边)，不新增 Journal/deny store。公共 corpus reader 同样允许支持的外部 selector，capture/evidence 注册在写前检查 sealability。

EvidenceBundle V2 复用原 ingestor 和嵌套 V1 validator，receipt 身份包含完整 binding、query、active-index、projection hash；即使 latency 不同，重复身份仍拒绝。V1 bundle 字节/digest 含义不变，回执和 fixture 均不代表正式算子贡献。

<a id="explicit-python-source-admission"></a>
## 显式 Python 源码准入

对于已审阅、不可变的部署，按绝对安装路径选择可信生成启动器，独立于包 import path。以下是路径模板；外层报告与 Knowledge 必需的独立报告各用新文件：

```sh
python -B /trusted/agent/python/generated_startup.py \
  --expected-python-modules /trusted/knowledge-modules.json \
  --startup-report /absolute/reports/bootstrap-startup.json \
  knowledge-cli -- \
  --repo-root /absolute/wiki/engine \
  --startup-report /absolute/reports/knowledge-cli-startup.json \
  search --request /absolute/requests/query.json
```

先从审阅字节准备 binding，再选择子进程 import root。`knowledge-cli`、`knowledge-mcp` 只要求 `kernelcat_knowledge`；`worker`、`ingestion` 要求四包。`--` 后参数和返回码保留。外层可选 typed JSON 报告有界、create-only、无隐式父目录/stdout；规范 parent 必须在每个准入包树外，物理目录身份可识别 parent symlink/native case alias。导出失败在派发前停止；中断可能留下部分文件，须检查而非覆盖或当作准入成功。

`preflight_modules(expected, required)` 只观察来源，不导入、不返回准入。可信装配禁用 bytecode 写入后导入固定包，再调用 `admit_modules(expected, required)` 复查字节与全部 loaded location，返回进程本地 `AdmittedModules`，其 `.report` 为分离数据；Agent 在导入入口后还调用 `.verify_loaded()`。序列化报告只是证据，不能替代观察。Worker/Knowledge 的生成 bundle 不导入其他能力包或另加 entry dispatcher；`scripts/generate_python_startup.py --check` 校验它们与单一 observer、纯 schema walker、O02 schema/types 一致。

仅支持普通文件系统 Python source；symlink、namespace/zip/custom/sourceless loader、产品 bytecode cache（含外部 cache prefix）、非普通/不可读源、O02 大小/路径/数量超限均拒绝。Binding root 必须为规范已有目录，身份 hash 相对 Python 路径和精确字节，版本/Git/timestamp 不建立身份。允许的相同字节搬迁保留 content/expected identity，报告实际新 origin。恶意 interpreter hook 和并发安装不在资格范围内。

Knowledge composition、CLI alias、MCP cache 都需要准入；产品 launcher 和 Journal durable adoption 是独立集成，单独此边界不等于 WD-O01 完成。

<a id="required-implementation-admission"></a>
## 必需的实现绑定

`build_application`、`get_application`、配置后的 `IndexLoader`、`LazyLoader` 及 legacy search wrapper 没有默认 binding。注入 repository/read model 也不绕过准入。直接构造 `WikiApplication` 要求 observer 的进程本地已检查对象，JSON report 不能代替。有限应用缓存先复查准入，再按规范 expected identity 与实际 origin 查找。

可信外层 bootstrap 可向 `main` 传 `expected_modules`；若还传 binding 文件，两者规范结果必须一致。外层可选报告与独立 CLI/MCP 必需报告分开。MCP model tool 无权选择 binding 或报告路径，绑定一个已准入 `KnowledgeMCP` 的方法。

使用任意旧 console wrapper 不属于此保护，应使用上文独立可信、由安装选定的 bootstrap。准入不证明模型/设备结果，也不替代 Worker/Node/Journal 的各自集成。

<a id="verified-operator-history"></a>
### 已验证的算子历史读取

`OperatorHistory` 卡片使用 `kc:history:operator:<canonical-id>:v1`，claim 绑定来源证据、raw presence/value、JSON/JSONL 字段地址及 manifest/parser/rule。Unknown/seeded 保留原因和 null interpreted value，recorded/unqualified 原样保留 raw value；历史 score 不代表当前执行资格。公开 schema 为 `contracts/knowledge/v1/operator_history.schema.json`。

发布走原 reviewed/conflict-checked 管理方，精确 ID/revision 与 audit 读可查看历史，四个 action-query bucket 在排名/限额前均排除它。字段地址不扩展 EvidenceRef selector，读者须另校验选中的来源字节；它不授权当前 reward 或新增存储管理方。

`WikiApplication.history_read(card_id, offset=0, limit=50)` 每次最多返回 100 条来源 claim，明确当前 parent/performance 资格；调用实际 `show`、`source_show`、`raw_read`，在验证的文档或精确物理 JSONL 行内解析严格 JSON pointer。Raw presence/value 必须一致，区分 missing/null/空字符串/false/0。Seeded 策略计数保留 raw 0、实测活动 unknown；空 parent digest 为 unknown；hidden delta 的已记录数值仍是历史值，不授权当前 parent 选择。

`card_publish(..., reviewed=True)` 准入前验证历史来源 claim。导入命令只产候选；有限 Worker 管理入口分开 `collect`、`admit --reviewed`、分页 `read`，显式使用外部 resource root 与 startup binding/report。候选卡和 atlas 在 card scan root 外，不隐式合并 `wiki/engine`。详细用法见 [Worker](../../apps/worker/README.md)。

Provenance 读用既有 raw 限额，单 selection 至多 4096 行/1 MiB。当前 loader 读整个有界卡，每页成本 O(卡 claims+所引 evidence+所选来源字节)，调用内缓存 selection。Import 单遍字段，按来源/行保留重复 trial ID，manifest 查找有索引。Atlas 最多 53 个排序的 canonical identity，唯一 alias 为 `conv2d -> conv_2d`。

该边界提供历史 KB00 查询，完整 KB01 work-order/implementation anchor 及旧 `intent.py` 等价仍未完成；历史生产命令、发布 backend、Git release 未改变。

`campaign-history/v2` 用 LF 物理行/CRLF 行尾读取 raw evidence，保留 JSON 字符串内 Unicode separator，要求 whole-file source scope。已注册 source subrange、旧 campaign/native selector 保持原语义；两个 campaign recipe 版本验证历史 claim 都要求绑定 campaign rule。Worker atlas 文件边界在准入/读前验证每个 entry 和 source identity/type，后部坏条目不能触发前部发布；整个批次发布仍非原子操作。
