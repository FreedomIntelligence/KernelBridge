<a id="kernelcat-ingestion"></a>
# Ingestion：固定来源采集与解析

`kernelcat-ingestion` 提供 `kernelcat_ingestion`，根据已审阅的 `SourcePlan` 执行一次有界采集和解析。它管理来源解释与派生快照；Executor 管理效果准入、预留和回执；Knowledge 管理不可变原文字节、来源与证据目录。

安装统一见[根 README](../../../README.md)。库通过注入的有限 effect/evidence 端口工作，不创建 Journal、Gateway、Knowledge 应用或具体供应商；导入不会创建文件、数据库、设备作业或模型请求。它不负责 Agent 循环、调度和知识卡发布。

<a id="commands-and-checks"></a>
## 先检查计划，再显式采集

完成根目录安装后，使用所选环境中的 `kc-data`。以下命令可在仓库根目录运行；所有 `/absolute/...` 均须替换为已审阅的实际路径，不依赖 cwd 推断来源：

```sh
PYTHONDONTWRITEBYTECODE=1 kc-data inspect --plan /absolute/plans/source-plan.json
```

输入是 UTF-8 JSON `SourcePlan`，最大 1 MiB；权威字段见 [plans.py](src/kernelcat_ingestion/plans.py)。必须明确计划名、每个来源的 kind/location/revision/source_class、资产列表、`approval_ref` 及归档用途。Git revision 为完整 40 位提交 SHA；非 Git 资产必须有审阅过的 SHA-256；本地根使用绝对路径。`inspect` 只验证和计数，stdout 输出 `status`、来源/资产数量、来源类型计数及 `executed: false`，不打开来源或初始化状态。

独立 `kc-data init/collect` 缺少具体 binding，会明确失败。实际初始化和采集由 Worker 组合已有 Executor/Knowledge：

```sh
python -B -P -m kernelcat_worker ingest \
  --expected-python-modules /absolute/config/four-package-modules.json \
  --startup-report /absolute/reports/ingestion-init-startup.json \
  init --wiki-root /absolute/corpus

python -B -P -m kernelcat_worker ingest \
  --expected-python-modules /absolute/config/four-package-modules.json \
  --startup-report /absolute/reports/ingestion-collect-startup.json \
  collect --plan /absolute/plans/source-plan.json \
  --state /absolute/ingestion-state --wiki-root /absolute/corpus
```

这里的 `python` 必须指向根安装流程选择的不可变普通 Python 源文件部署。Binding 独立准备，精确绑定 `kernelcat_worker`、`kernelcat_executor`、`kernelcat_knowledge`、`kernelcat_ingestion` 四包；当前 Executor checkout 的资源布局不满足直接源码准入，不能用临时 `PYTHONPATH` 绕过。详见 [Python 绑定合同](../executor/src/kernelcat_executor/contracts/python_modules.py)和 [CI 部署说明](../../../validation/DELIVERY_GUIDE.md)。

报告父目录须已存在，报告使用新的绝对文件名并位于所有准入包目录之外；导出为 create-only，不覆盖、不自动建父目录。`init` 必须提供 `--startup-report`，用于显式创建新的外部 corpus；`collect` 可选导出报告，以上示例仍显式保留它。`--state` 和 corpus 都必须位于源码 checkout 之外，采集使用已初始化的 corpus。不要对已有来源库重复执行初始化，也不要改写历史状态指纹来恢复。

成功输出一份规范 JSON：初始化返回 `status: initialized` 和 `wiki_root`，采集返回包含来源结果及派生对象引用的报告。只有 `initialized`、`valid`、`completed` 退出码为 0，错误或未完成为 2。状态目录保留 `plan.json`、派生对象和原 Journal；原文字节、来源/证据记录保存在指定 corpus。报告保留 partial、failed、unknown，标记 `source_derived` 和 `target_validation=not_performed`，不代表目标设备验证。

支持明确列出的本地/run 文件、现有本地 Git commit，以及显式授权后的有界 HTTPS。HTTPS 需要计划中的正数 requests 配额和 `collect --authorize-network`；上面的本地示例未授权网络。解析不会执行源代码、hook、notebook 或安装脚本。

<a id="worker-binding-specification"></a>
## Python 嵌入与 Worker 端口

以下是调用片段，所用 Path、已校验计划和两项端口须由可信调用方准备，不能直接作为独立脚本运行：

```python
from kernelcat_ingestion.pipeline import Pipeline

pipeline = Pipeline(
    state=state_directory,
    plan=reviewed_plan,
    effects=worker_effect_binding,
    evidence=worker_evidence_adapter,
    authorize_network=False,
)
report = pipeline.collect()
```

具体装配位于 `apps/worker`；公开协议在 `kernelcat_ingestion.ports`：

- `EffectBinding.bind(task, tools, *, credits, resource_limits, runtime_digest)` 返回 `BoundEffects`。Worker 创建原 Journal/Gateway、初始化正数资源维度、绑定运行时身份并注册任务。
- `BoundEffects.execute(action)` 调原 Gateway；`snapshot()` 返回其 Journal 投影；`task_lock(task_id)` 持原任务锁。投影保留既有 `actions`、`resources` 形状。
- `EvidenceStore` 只暴露 `capture`、`source`、`read_source`、`register_evidence`、`evidence`。Worker 打开 Knowledge 公共 corpus，并将 `CorpusError` 转为带原异常链的 `DataError`。
- `WikiEvidence(root, *, store)` 在注入的 store 上转换来源/Element 身份；`identity` 为明确解析的 root，不初始化、不发现供应商、不发布。
- `IngestionCommands.initialize(root)` 与 `collect(state, evidence_root, plan, *, authorize_network)` 绑定 CLI；`cli.main(argv, commands=...)` 接收该绑定。

Worker 将自身、Executor、Knowledge 实现身份与 Pipeline 的 `runtime_digest` 一起固定。摘要绑定 Ingestion 代码、解析环境、完整计划、证据根和网络授权；改变这些输入需要新外部状态，历史指纹不重写。原 task/action ID、source ID、selector 保留。重用完成回执后仍核对现存来源及证据字节；UNKNOWN 保留原预留并在重发前停止。本地 requests=0 计划只使用既有正数 bytes 配额。

<a id="parser-interpretation-identity"></a>
## 解析身份与限额

新解析使用 `native-lossless/v2`。`Parsed.parser` 为该前缀加规范 JSON，记录环境、有效选项、来源名/后缀、role、产物摘要。每个 Element 的 `context.parser_identity` 包含 recipe、descriptor 和 SHA-256；EvidenceID 绑定该摘要、原 source、selector、kind。Knowledge 保留同一 descriptor/digest 及完整 Element 的 `interpretation_digest`，不可变冲突与重复规则不变。空、失败、不支持结果也保留身份。

`observe_environment()` 在解析边界做本地观察；公共 `parse` 不接受调用方声称的环境。Pipeline 固定一个不可变 `NativeParser`，在采集前将环境纳入原 runtime binding，每次新解析前复查。环境改变由原 Journal 拒绝旧状态。历史 v1 Parsed、EvidenceRef 与 `kernelcat-data/native-lossless-v1` 生产者保留原身份，可直接读而不重算。

[parsers/identity.py](src/kernelcat_ingestion/parsers/identity.py) 明确以下观察闭包：

- Python 实现/完整版本、默认语法 major/minor、解释器及共享/framework runtime 字节；递归、整数转换、CSV 字段限额；AST mode/type-comment/feature-version、UTF-8 BOM、`chunk_lines` 的有效选项。
- Native parser、身份 recipe、计划模型、Executor 规范序列化；`ast`、`csv`、`io`、`pathlib`、JSON 编解码/扫描、regex 与直接 CSV/JSON 扩展的模块字节、已加载函数/方法及默认值/闭包。
- `codecs`、UTF-8/UTF-8-sig、有效缓存 decoder、strict 错误处理和 `codecs.BOM_UTF8`；纯 PyYAML loader 的模块字节/代码、版本、constructor/resolver 表、Boolean 值、scanner 转义、tag handle、fallback tag、不可打印模式。此路径不用 C YAML loader。
- Pydantic 与 `pydantic_core` 完整 Python/native 包文件，以及已加载的 `pydantic.main` 方法，绑定实际校验/序列化实现。

身份使用逻辑名及摘要，不含绝对安装路径、时钟、PID 或随机值。单文件观察上限 128 MiB；每个 Pydantic 包闭包至多 1024 文件；最终环境 JSON 上限 16 KiB，只保留组件摘要。

每个组件使用局部引用图，保留 list/tuple、字节内容、binary64 浮点表示（含 NaN sign）与字典插入顺序，重复引用复用节点。支持精确 primitive、list/tuple/dict、Python 函数/default/closure、regex、内建函数/slot、itemgetter 参数、空 set、已知 runtime type 及 `dataclasses.MISSING`/`PydanticUndefined`。非空 set、用户对象等不支持值在解释前抛 `ObservationError`，不调用任意 repr/reduction/property 猜测身份。

每组件最多 4096 遍历边、深度 32、保守表示预算 256 KiB；先计元数据与最坏 JSON payload 空间，再分配或转换。共享图遍历对访问节点/边和 scalar 字节线性，结束即丢弃引用表。

函数代码 normalize/marshal 前另做有界准入：最多 4096 输入边、深度 32、256 KiB 代码/常量表示，涵盖 bytecode、exception/line table、name、constant。共享 CodeType 节点和常量容器中的 CodeType 不支持，普通直接嵌套函数代码可用；支持的 tuple/frozenset 常量引用只访问一次并保留 marshal 引用编码。超限在复制/序列化/解释前失败，代码图预算与闭包预算分开。

原 16 MiB 输入、4096 Element、32 MiB 输出、8192 symbol 限额仍含身份元数据。观察成本为有界文件字节/已加载代码的线性处理加文件/模块排序，不按 Element 重复。该闭包不覆盖任意 Python plugin、import hook、native 热替换或恶意并发修改；依赖和解释器文件须保持可读，缺失即失败，不退回版本标签或缓存。来源身份不是设备验证。

<a id="responsibility-layout"></a>
## 代码结构和验证

`plans.py` 定义计划与解析值对象；`sources` 采集，`parsers` 解释固定字节，`pipeline.py` 组合一次有限操作，`snapshots.py` 保存可重建派生对象，`ports.py` 保留注入边界。公开模块按来源采集、解析、计划和存储职责组织。

使用[根 README 的回归命令](../../../README.md#独立安装与开发)验证交付。Ingestion 的有限 API 保留原来源身份、请求/字节配额和回执；`smoke(output, commands=...)` 提供离线导入夹具。

<a id="pinned-operator-history"></a>
## 固定算子历史

Worker 的有限 `operator-history collect` 接收显式 source root 和 `wiki/catalogs/operator-history-sources.json`，目录使用可移植相对路径与 A02 内容 hash，不授予动态爬取或发布权限。实际命令与输入见 [Worker 说明](../../apps/worker/README.md)。

`CampaignParser` 通过原 Pipeline、Gateway/Journal 和注入证据存储保留 JSON 文档及物理 JSONL 行。空行不改记录行号，相同 trial ID 按来源/行分别保留。`campaign-history/v2` 只按 LF 分行，CRLF 去掉尾 CR，只跳过纯 JSON whitespace 空行；字符串中的 U+2028/U+2029 仍是数据，vertical tab 不能分隔或隐藏记录。Knowledge raw read 使用相同行语义，旧 campaign/native recipe 保留原 selector。重复 JSON key、非有限数字和坏记录均拒绝。

本地采集 requests=0，保留正数 byte/credit 上限与原 gate 成本。UNKNOWN/回复丢失继续保留原 Journal 预留，缓存完成仍复查来源/证据字节。只有所有条目完成后才输出候选和 atlas；缺失/变化的 pin 输出不完整逐来源报告。Manifest、parser/rule 或固定 batch timestamp 改变须使用新的外部状态目录。历史 timestamp 原字符串保留；原始历史字节只进入配置的外部 CAS，运行输出不入 Git。
