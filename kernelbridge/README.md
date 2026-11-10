# KernelBridge

CANN 任务统一从 [task 提交入口](../examples/cann/README.md) 启动：冻结 task 与测试输入，建立 LoopX Goal，再由 Pi SDK 执行算子生成、NPU 评测和修复。默认复用 managed SDK + LoopX；`--mode generate` 提供单次 Pi 会话。Vector Add、Cube Matmul 与 Sigmoid 40-turn 复现包含原生 C++、profile、完整日志索引和 session。

KernelBridge 为 KernelAgent 提供三项能力：**Context 组织本次模型输入，Search 检索知识与原始证据，Replay 回放分析历史执行过程。** 用于诊断算子、形成实验假设、核对结果和复盘失败原因。

V1 通过 KernelCat 的配置、CLI 和 TypeScript SDK 接入，复用 KernelCat 会话执行与 Worker。安装及首次运行统一见 [根目录 README](../README.md)，任务输入、模型配置和 SDK 用法见 [KernelCat README](../kernelcat/README.md)。

## SDK 快速开始

KernelBridge 的 Context 和 Search 随 KernelCat SDK 会话接入；Replay 使用本页的只读分析命令。应用直接调用随包的 SDK 源码入口，安装统一按根 README 完成。

以下以随包 CPU 向量加法为例，从任务输入走到一次真实 SDK 调用。所有 `/absolute/...` 都替换为本机绝对路径。换算子时，任务合同、父程序、case 与独立评估器需要成套准备。

### 1. 准备任务和运行配置

在交付根目录执行，输出目录必须尚不存在：

```sh
python3 -I -B examples/vector-add/run.py \
  --bundle /absolute/KernelBridge \
  --output /absolute/runs/bridge-sdk-001 \
  --prepare-only
```

该步骤生成 `task.json`、`config.json` 和固定评估器，不请求模型。先检查任务中的 `goal`、`contract` 和 `seed_files`；模型服务及凭证环境变量在生成的配置中选择。按根 README 注入该环境变量，再创建本次共享账户：

```sh
./kernelagent account-create \
  --config /absolute/runs/bridge-sdk-001/config.json \
  --intent bridge-account-001 --credits 180 --multi-trial \
  > /absolute/runs/bridge-sdk-001/account.json
```

账户命令返回原始账户身份，不调用模型。知识根接入的账户要求见下文 Search；本例使用随包默认配置。

### 2. 把任务和上下文交给 SDK

把以下代码保存为 `/absolute/runs/bridge-sdk-001/run-sdk.mjs`。它读取准备好的任务、原账户及配套合同，先保存本次明确输入，再创建会话、读取事件并取回结果。

```js
import { readFile, writeFile } from "node:fs/promises";
import { KernelCat } from "/absolute/KernelBridge/source/apps/agent/src/sdk/index.ts";

const bundle = "/absolute/KernelBridge";
const input = "/absolute/runs/bridge-sdk-001";
const task = JSON.parse(await readFile(`${input}/task.json`, "utf8"));
const accountReceipt = JSON.parse(await readFile(`${input}/account.json`, "utf8"));
const contractText = await readFile(`${bundle}/examples/vector-add/task-contract.json`, "utf8");
const context = "补充说明：保持两个输入不变，先检查零长度和不规则尾部。";
const manifest = {
  schema_version: "kernelcat.run-launch/v1",
  account_id: accountReceipt.account.account_id,
  intent_id: "bridge-launch-001",
  run_id: "bridge-run-001",
  trial_id: "bridge-trial-001",
  branch_id: "bridge-branch-001",
  session_id: "bridge-session-001",
  seed: 17,
  goal: `${task.goal}\n\n参考说明：\n${context}`,
  contract_source_text: contractText,
  program: {
    schema_version: "kernelcat.program-bundle/v1",
    entrypoint: "solution.js",
    configuration_source: "{}",
    files: [{ path: "solution.js", text: task.seed_files["solution.js"] }],
  },
  proposal: {
    proposal_id: "bridge-proposal-001",
    conditions: "配套父程序和独立评估器已准备",
    operation: "检查源码，形成一个明确假设并用原评估器验证候选",
    expected_outcome: "保留候选及原始正确性和测量回执",
    stop_conditions: ["原模型步数或 Trial 额度耗尽", "本次任务完成"],
    parameter_source: "{}",
  },
  model_route: "primary",
  run_credit_limit: 180,
  trial_credit_limit: 30,
};
const manifestPath = `${input}/run.json`;
await writeFile(manifestPath, JSON.stringify(manifest, null, 2), { flag: "wx" });

const client = new KernelCat({
  python: `${bundle}/runtime/venv/bin/python`,
  config: `${input}/config.json`,
  expectedPythonModules: `${bundle}/runtime/expected.json`,
  timeoutMs: 450000,
  maxFrameBytes: 1048576,
});
const session = await client.createSession({ manifest: manifestPath });
try {
  const invocation = session.runStreamed();
  for await (const event of invocation.events) {
    console.log(event.sequence, event.type, event.payload);
  }
  const result = await invocation.result;
  console.log(result.status, result.runtime_state);
  await writeFile(`${input}/sdk-result.json`, JSON.stringify(result, null, 2), { flag: "wx" });
} finally {
  await session.dispose();
}
```

用安装选定的 Node 执行：

```sh
/absolute/node /absolute/runs/bridge-sdk-001/run-sdk.mjs
```

这一步会调用配置的模型和评估适配器，受原账户、Run、Trial 与模型步数限制。只执行一次；示例用 `wx` 防止覆盖已提交输入。保留输出目录和原回执处理失败或丢失确认，不反复执行脚本重发同一效果。

不需要事件流时，把 `const invocation = session.runStreamed();` 起，到 `const result = await invocation.result;` 止的整段代码（包括事件循环）替换为 `const result = await session.run();`；保留后面的结果处理和 `finally` 清理。首次任务来自 manifest；运行中的补充使用带当前状态版本的请求，见 [KernelCat 会话输入](../kernelcat/README.md#在执行中补充说明)。

### 3. 恢复同一任务

保留原 manifest，并根据原运行状态准备稳定的会话替换请求。以下片段复用相同 Worker 配置的 `client`，可在新进程中重新创建该 client：

```js
const restored = await client.selectSession({ manifest: "/absolute/runs/bridge-sdk-001/run.json" });
try {
  const result = await restored.run({ resume: "/absolute/replace-session.json" });
  console.log(result.runtime_state);
} finally {
  await restored.dispose();
}
```

`resume` 可传稳定请求文件或完整 `ReplaceSessionRequest`。手动下一 Trial 使用 `nextTrial`；自动多 Trial 使用 `pursueGoal("/absolute/pursuit.json")`，由原 LoopX Goal 逐轮准入。账户、请求格式和绑定示例见 [KernelCat README](../kernelcat/README.md)。

## 与 KernelCat、LoopX 和 RSI 的关系

| 组件 | 负责什么 |
| --- | --- |
| KernelCat | 接收算子任务，管理会话、模型、工具与子 Agent，生成候选并调用配置的评估器 |
| KernelBridge | 组织上下文，提供知识与证据读取，分析历史会话 |
| LoopX | 持有目标、Todo 和配额，决定自动优化的下一 Trial 是否获准执行 |
| RSI | 在执行中选择知识、提出候选与实验、分析评估反馈 |

围绕目标自动推进多个 Trial 使用 `pursue --goal`，由 LoopX 驱动；单次执行或操作人明确提交的一步使用 `run`。RSI 不另起自主调度循环。KernelBridge 的知识与回放配置不改变 LoopX 的目标和配额管理；所有效果与预算仍由原始 Gateway/Journal 准入和记账。

## 任务和上下文从哪里输入

任务由 KernelCat 的任务 JSON 或托管 manifest 提交；KernelBridge 在同一次执行里处理这些输入。首次启动前，先把目标、算子约束、候选源码与参考资料准备完整：

| 要给 Agent 的内容 | 放在哪里 |
| --- | --- |
| 要做什么、完成条件和额外解释 | 独立任务的 `goal`；托管 manifest 的 `goal` |
| shape/dtype、正确性和目标平台约束 | 独立任务的 `contract`；托管的 `contract_source_text` |
| 可修改的候选源码 | 独立任务的 `seed_files`；托管的 `program.files` |
| 一小段背景或参考说明 | 调用方读取文本后，在首次启动前写入任务说明 |
| 已管理的大量来源与原始证据 | `knowledge_root`，再用结构化查询读取所需内容 |
| 执行中的补充问题或调整 | 活跃会话 `ready` 之后提交带当前 stamp 的 `prompt`、`steer` 或 `followUp` 请求 |

文件路径用于选择文件；需要作为说明输入的文本，要由调用方显式读取并写入任务，或通过获准的知识/文件读取工具提供。参考实现与评估器保持在候选可写范围之外。

完整任务 JSON、SDK 首次运行、事件读取和恢复示例见 [KernelCat：任务与上下文输入](../kernelcat/README.md#任务与上下文输入)。安装后的运行方式如下。

## 接入方式

先按根 README 完成安装，并准备自己的任务与配置。以下命令均从交付根目录执行。

一次独立任务：

```sh
./kernelagent run --config /absolute/config.yaml --task /absolute/task.json
```

已有托管 manifest 的一次执行：

```sh
./kernelagent run --config /absolute/config.yaml --manifest /absolute/run.json
```

围绕已有 Goal 自动推进：

```sh
./kernelagent pursue --goal /absolute/pursuit.json
```

`pursuit.json` 的绑定格式见 [KernelCat 的 LoopX 入口](../kernelcat/README.md#loopx-自动续接入口)。SDK 使用 `new KernelCat({ config, ... })`，或在 `pursueGoal()` 的绑定文件中指定 `worker.config`；都由同一配置接入 KernelBridge。

Context 随执行处理输入。知识检索通过 `knowledge_root` 接入已有资源；只读 Replay 使用下文的 `session-replay` 命令。

## Context：让模型拿到本次需要的资料

Context 接收任务约束、当前预算、受保护资料、工具观察和完整对话交换，按配置的字节预算组织模型输入。必需内容优先保留，可选历史按完整交换选择；必需输入超限时，在请求模型前返回错误。

可以在已有 Worker 配置中设置上下文预算：

```yaml
policy:
  context_bytes: 65536
```

这是配置片段，应合并进自己的完整配置，保留其中的模型、运行时和其他策略字段。`context_bytes` 是字节上限，与模型 token 上限分别配置。

上下文清理保留用户约束、近期模型轮次、失败或未决的工具交换。具备原始完成证明的旧成功输出可以使用不可变 receipt 引用替换；原始 Journal 与会话 JSONL 继续保留，可通过受控的 `read_receipt` 读取原回执。清理过程不额外调用摘要模型。

提交模型前，实际输入字节、摘要和当前执行版本会经过原始准入检查。原始 Journal 保留实际模型输入及其摘要，可与会话历史、知识查询和工具回执关联，核对一次决定依据了什么。

## Search：带着问题查知识，再读原始依据

Search 查询已配置的 Knowledge 资源根，返回与算子或机制、执行阶段和当前问题匹配的知识。推荐使用具体问题，例如“能否减少重复读取”，再沿返回身份核对原始证据。

### 配置知识来源

在已有 Worker 配置中加入准备好的资源根，并保持知识、运行状态和候选工作区分离：

```yaml
knowledge_root: /absolute/knowledge-root
state_dir: /absolute/task/state
workspace: /absolute/task/workspace
```

`knowledge_root` 指向已有的 Knowledge 资源目录，资料及证据由 Knowledge 管理。使用绝对路径便于核对实际来源。目录必须与可写的 state/workspace 分离；托管模式还会检查其与控制器、合同和评估器目录的边界。

独立 `run --task` 可接入该根的只读工具。托管 Run/Trial 使用保留视图：账户须为具有匹配 Knowledge authority pin 的新 v3 `--multi-trial` 账户，并绑定相应配置根。账户创建与 manifest 用法见 [KernelCat README](../kernelcat/README.md)。运行时按原始身份检查知识视图及撤回状态。

### 查询与读取接口

接入后，Agent 可在获准的工具目录中发现以下四个只读工具：

| 工具 | 输入 | 用途 |
| --- | --- | --- |
| `knowledge_search` | 结构化 `request` | 按当前问题检索知识，取得查询回执 |
| `knowledge_show` | `knowledge_id`，可选 `view`、`max_chars` | 查看逻辑知识页或精确修订；`view` 支持 `agent`、`audit`、`human` |
| `knowledge_source` | `source_id` | 查看已注册来源身份与元数据 |
| `knowledge_read` | `evidence_id` | 校验来源字节与证据选择器后，读取原文片段 |

`knowledge_search` 的工具参数示例：

```json
{
  "request": {
    "schema_version": "kernelcat.knowledge-query/v1",
    "operator_id": "vector_add",
    "phase": "performance",
    "bottleneck": "memory traffic",
    "decision_question": "Can redundant input reads be removed?",
    "limit": 3,
    "token_budget": 1200
  }
}
```

将 `operator_id` 替换为资源库中对应的算子身份；也可改用 `mechanism_family`，两者选一。`limit` 为 1–10，`token_budget` 为 256–1200。示例描述查询格式，返回内容取决于实际接入的资料。

KernelCat 的 `tool_search` 用来发现当前工具目录；知识内容由 `knowledge_search` 查询。随后用 `knowledge_show` 查看返回的精确修订，按其 `evidence_refs` 调用 `knowledge_read`，再用 `knowledge_source` 核对来源。

### 如何理解结果

查询结果包含 `projection` 和 `receipt`。投影将资料分为约束、建议、不要重复的失败方向和测量规则；知识保留证据引用与证据成熟度。回执关联查询摘要、索引修订、返回卡片及其精确修订、投影摘要和语料绑定。

引用知识后，仍须由任务配置的独立评估器验证候选正确性与性能。检索回执说明“查到了什么”；候选采用、验证与测量依据各自的原始回执核对。

配置 RSI 知识准备时，将完整的结构化查询放入现有 `rsi.knowledge_query`，同时配置 `knowledge_root`。保留原 RSI 配置中的源码范围、固定评估适配器、内容摘要、策略和预算。知识准备结果与实验观察保存在原始记录中，用于核对本轮输入、候选变化和评估反馈。

## 自研工具、审查与执行记录

会话通过获准工具目录发现并调用自研工具，工具权限和预算由原 Gateway/Journal 检查。`tool_search` 发现工具，`codemode` 组合调用；内建源码读写工具处理候选文件，配置 RSI 后 `run_experiment` 请求固定评估适配器，知识工具按本页 Search 接入。

依赖版本以 `source/apps/agent/package.json` 和 `package-lock.json` 为准，按根 README 一次安装。业务工具由受信任 Worker 装配，随后通过原生工具目录暴露；可选工具是否出现取决于本次配置和准入，安装包存在不代表已开启。

现有底层 SDK 接入点对应以下职责：

| 接入点 | 当前用途 |
| --- | --- |
| `registerProvider` | 将模型请求转交受保护控制器和原始 Gateway |
| `createMcpExtension`、`createCodemodeExtension`、`createToolSearchExtension` | 激活同一工具目录、发现与组合调用 |
| `session_start`、`session_shutdown` | 在会话生命周期内管理扩展状态与幂等释放 |
| `agent_before_settle` | 根据已获准 checkpoint 的一次继续许可衔接会话内下一 turn |
| `agent_settled` | 已结束的通知边界，不准入新工作 |

应用公开调用入口是 `source/apps/agent/src/sdk/index.ts`；自研业务工具的装配入口是 `source/apps/worker/src/kernelcat_worker/bindings.py`。新增工具需要在受信任装配层提供参数合同和效果准入；调用方通过现有 SDK 使用配置好的工具。

需要源码审查时，在任务 `goal` 中要求调用 `delegate`，选择 `kernel-reviewer` 和 `mode: "read_only"`；`kernel-scout` 用于受控资料分析。子 Agent 共享原账户预算，只读模式不授予写文件、启动设备实验或再委派权限。检查原始 `delegate` 完成回执以确认审查发生；审查意见与评估器的正确性/测量回执分别判断。

RSI 保存实际查询、候选、知识选择、上下文及实验记录，可据此核对“查到什么、选了什么、改了什么”。知识是否导致性能收益，需要对应任务、模型、环境和独立评估证据；一次检索或审查结果不替代这些证据。

## IR、二进制和 trace 怎样作为输入与证据

| 材料 | 本版可用方式 | 依据如何判断 |
| --- | --- | --- |
| 文本 IR、反汇编摘录、trace 分析片段 | 作为短文本写入任务说明，或注册为 Knowledge 来源/证据后读取 | 核对采集环境、源码与构建身份；文本用于分析假设 |
| 设备二进制和构建产物 | 在显式产物清单中记录路径和 SHA-256，用 Replay 校验字节 | 字节一致性与来源关联分别核对，不能由摘要推出性能 |
| 实验计时与性能诊断文件 | 保留原评估器回执、原始样本和采集范围，并按原始身份引用 | 区分普通计时与 profiling 采集，核对独立确认和环境 |

产物清单格式以及可执行的检查命令见下文。KernelBridge 处理的是明确提供的文本、原始证据与产物身份；NPU 任务由配置的独立设备评估器执行。

## NPU 任务应提供哪些输入

任务合同中提供明确的算子语义、具名输入/输出 case、shape、dtype、layout；`layout: "strided"` 时同时给出 strides。目标硬件、SoC、设备、编译器/运行时及独立评估器按本次真实环境固定。

shape 分支、owner 归属和 batch 分派属于具体候选实现与评估配置：在任务说明中列清覆盖范围，并用对应 case 检查，不能仅由字段存在推定后端已经支持。候选、参考实现、评估器、原始作业与回执保留各自身份。托管 SDK 的设备评估接入点是完整 `rsi` 配置中的 `adapter_argv` 与 `adapter_files` 内容摘要，适配器按原 `AdapterRequest/AdapterResult` 合同工作。设备执行仍经原 Gateway 准入；先核对环境与正确性，再解释实际测量。

## Replay：只读回放历史会话

Replay 读取冻结的会话 JSONL，输出结构化分析报告。可用于查看历史用户约束、模型与工具交换、会话分支关系，以及尚未配对的调用或未决观察。

准备输入副本和已存在的报告目录，再执行：

```sh
./kernelagent session-replay \
  --input /absolute/frozen/session.jsonl \
  --output /absolute/reports/session-analysis.json
```

输入与输出使用无符号链接的规范绝对路径。输出文件必须尚不存在，其父目录须已创建，且位于输入目录、产品源码目录和历史记录中的模型工作区之外。

需要核对某一前缀或分歧点时，可指定原始行号：

```sh
./kernelagent session-replay \
  --input /absolute/frozen/session.jsonl \
  --output /absolute/reports/session-prefix.json \
  --prefix-line 200 \
  --divergence-line 150
```

`prefix-line` 表示最多保留到哪一行；`divergence-line` 表示从哪一行开始排除。上例分析至第 149 行。行号必须落在该输入允许的范围内。

### 关联 IR、二进制或 trace 产物

把已经取得的产物保存到输入目录之外，另建清单，SHA-256 使用真实文件字节计算的 64 位十六进制值：

```json
{
  "artifacts": [
    { "path": "/absolute/artifacts/kernel.ir", "sha256": "REPLACE_WITH_ACTUAL_SHA256" },
    { "path": "/absolute/artifacts/kernel.so", "sha256": "REPLACE_WITH_ACTUAL_SHA256" },
    { "path": "/absolute/artifacts/trace.json", "sha256": "REPLACE_WITH_ACTUAL_SHA256" }
  ]
}
```

这是待填写的清单模板；将每项替换为本次实际文件和摘要，再运行：

```sh
./kernelagent session-replay \
  --input /absolute/frozen/session.jsonl \
  --output /absolute/reports/with-artifacts.json \
  --artifacts /absolute/artifacts-manifest.json
```

报告逐项记录 `verified`、`unavailable` 或 `conflict`。它验证当前文件字节，不证明该文件在历史会话当时存在，也不执行二进制、解释 IR 指令或采集 trace。

### 报告包含什么

- 输入文件摘要、分析前缀和原始行/字节引用。
- 消息、工具调用及返回身份，以及可识别的会话和分支关系。
- `current_view.pending_call_ids` 与 `pending_handles`，帮助定位需要对账的原始操作。
- 历史用户消息位置、工作区观察和能够关联的原始产物引用。

CLI 同时打印摘要与计数，完整报告写入指定 JSON。该命令只读历史，不调用模型、执行历史命令、提交实验或继续会话。报告中的运行状态是历史观察；需要继续真实任务时，按 [KernelCat 的恢复流程](../kernelcat/README.md#中断与恢复) 查询原始状态与回执。

## 在一次算子任务中怎么使用

1. **诊断前**，由 Context 带入任务约束、当前源码、预算和保留观察。
2. **形成假设时**，用 Search 检索具体问题，读取知识修订和原始依据。
3. **实现与实验时**，把假设写进明确任务说明，请只读子 Agent 审查后生成候选，通过配置评估器取得原始回执。
4. **评估后**，核对源码、case、正确性、计时和环境，保存本轮结论。
5. **复盘时**，用 Replay 定位当时的输入、工具结果和未决调用。
6. **继续时**，由原始执行入口恢复；自动下一 Trial 仍由 LoopX 准入。

原始 Journal、会话 JSONL、任务合同、候选源码和评估回执一起构成任务记录。分析报告是这些记录的只读索引，应与原始身份一起保存。

## 按阶段接入自己的 workflow

| 阶段 | 先做什么 | 核对的结果 |
| --- | --- | --- |
| 跑通任务与评估器 | 按根 README 完成安装与 CPU 示例离线检查 | 输入、父程序、正确性及测量回执对应同一组文件 |
| 接入 SDK、资料和审查 | 使用本页 SDK 示例，准备 `goal`、合同、知识根和只读审查要求 | 原始事件、知识查询与审查完成回执可追溯 |
| 接入自己的设备评估 | 固定目标环境、case 和受保护的 `rsi.adapter_argv`/内容摘要，再执行一次受准入实验 | 候选、设备、环境、原始作业、正确性和计时身份完整 |
| 持续任务与复盘 | 保留原 Goal/Run/Journal，显式恢复或由 LoopX 准入下一 Trial，用 Replay 核对过程与产物 | 无未决效果被重复发送，结论可沿原始回执复核 |

每一步先验证当前输入与实际回执，再进入下一步。批量任务的设备与作业划分由调用方及相应评估器明确提供；工具记录、审查意见、构建成功和性能结论分别保留依据。

## 常见问题

| 现象 | 检查方式 |
| --- | --- |
| 模型看不到知识工具 | 检查实际配置的 `knowledge_root`，以及该任务获准的工具目录 |
| 查询结果为空 | 检查算子/机制身份、阶段和问题范围；通过返回的索引及语料绑定确认所查资料 |
| 知识原文读取被拒绝 | 按来源、摘要和证据身份核对资源；保留错误，不以缓存文本代替通过校验的证据 |
| 托管知识接入报冲突 | 检查账户版本、Knowledge authority pin、配置根及视图身份 |
| 输入超过上下文上限 | 缩减可选资料或在创建任务前调整配置，保留必需约束 |
| 回放报告拒绝写入 | 检查输出是否已存在，父目录是否存在，以及路径是否满足分离要求 |
| 报告发现未决操作 | 通过原始状态与回执对账，沿用原意图；不能通过回放报告判断其已完成 |

所有功能配置应在任务与运行输入被冻结前确定；继续已有任务时保留其原始身份、配置绑定和未决操作记录。

历史会话支持 [一条命令恢复](../examples/cann/README.md#一条命令恢复原会话)：校验归档后，以 Pi SessionManager 还原原 session ID、当前分支与完整上下文，输出 `session.html`、`session.jsonl` 和原生 Pi 打开命令。Sigmoid 的重新实验命令与历史会话恢复命令分别列在 [复现指南](../examples/cann/sigmoid-optimization/README.md)。
