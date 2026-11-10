# KernelCat

CANN 任务统一从 [task 提交入口](../examples/cann/README.md) 启动：冻结 task 与测试输入，建立 LoopX Goal，再由 Pi SDK 执行算子生成、NPU 评测和修复。默认复用 managed SDK + LoopX；`--mode generate` 提供单次 Pi 会话。Vector Add、Cube Matmul 与 Sigmoid 40-turn 复现包含原生 C++、profile、完整日志索引和 session。

KernelCat 是面向算子生成的 CLI 与 TypeScript 应用 SDK。输入明确的算子语义、shape/dtype、目标平台、正确性要求及预算；Agent 在许可范围内读取资料、生成候选，并通过配置的评估器验证结果。安装与第一个独立示例统一见 [交付根目录 README](../README.md)。

## 执行入口

围绕一个目标自动推进多个 Trial，默认使用 `pursue --goal`，由 LoopX 管目标、Todo、配额和下一轮准入。`run` 是单次执行或操作人明确提交的一步；同次执行里的模型、工具、对话续接与子 Agent 由 KernelCat 会话管理。RSI 提供优化动作和反馈，不另起自主调度循环。Replay 独立开关，关闭它不关闭 LoopX 的目标与配额管理。

| 入口 | 准备的输入 | 用途 |
| --- | --- | --- |
| `run --task` | 独立任务 JSON 与 Worker 配置 | 执行一次任务 |
| `run --manifest` | 已有账户、冻结任务合同与 Run manifest | 执行一次托管 Trial |
| `run --manifest --resume` | 原 manifest 与稳定的会话替换请求 | 在同一 Trial 下恢复获准执行 |
| `run --manifest --next-trial` | 原 manifest 与明确的下一 Trial 请求 | 操作人提交下一次尝试 |
| `pursue --goal` | 已有 Goal/Agent/Todo 和完整绑定 JSON | 由 LoopX 决定自动下一 Trial 的准入 |
| `runtime-state` | 原 Run 与 Trial 身份 | 读取当前状态，不发起模型请求 |

这里的 Goal 是持续目标，Run 是具有累计预算的一次任务运行，Trial 是 Run 内一次候选尝试；manifest 是冻结任务、父程序和运行输入的 JSON 文件。账户、原始 Journal 与各请求的意图身份在恢复时继续沿用。

在交付根目录运行：

```sh
./kernelagent --help
./kernelagent run \
  --config /absolute/run/config.yaml \
  --task /absolute/run/task.json
```

`kernelagent` 使用安装记录转交实际 TypeScript CLI。普通任务和控制命令复用安装选定的 Python 与 `expected.json`；`pursue` 从绑定 JSON 的 `worker` 字段读取配置，`session-replay` 直接运行只读 Node 分析脚本。KernelCat 执行对话，受保护父进程处理上下文及受控继续；Python Worker 提供有限效果、校验、预算准入和原始回执。运行时不扫描或安装用户插件，也没有第二套 Python Agent 循环。

`--task` 是独立任务入口。任务和配置都需绝对路径；完整可运行模板由根目录的向量加法示例生成。直接运行 `source/apps/agent/src/main.ts` 时还需显式传入 `--python` 与 `--expected-python-modules`，通常使用已绑定的启动器即可。

| 输入 | 客户需要明确的内容 |
| --- | --- |
| 任务 | 算子语义、输入范围、初始程序、允许修改的源码及验收要求 |
| 模型 | `providers` 中的路由名、服务协议、地址、模型 ID、凭证环境变量名与请求上限 |
| 目标平台 | 原生编译器、设备运行时及评估器所需的环境；CPU 示例不代表设备配置 |
| 权限与预算 | 可写工作区、允许调用的效果、总 credits、模型步数、超时与评估额度 |
| 受保护配置 | 模块绑定、会话运行时路径、独立评估器及其固定内容摘要 |

执行命令在终端输出结构化 JSON，诊断信息写入标准错误；`--help` 输出命令帮助。候选源码和评估回执的位置由任务配置与运行状态指出。

配置的 `python_modules` 应等于所选安装 `expected.json` 的 JSON 内容；示例生成的会话运行时路径应指向本包 `source/apps/agent` 和安装选定的 Node。`state_dir` 和 `workspace` 使用客户数据目录。保持评估器、参考实现、任务合同及控制配置在候选可写范围之外。

模型服务通过显式 `providers` 配置接入，支持配置合同中的 OpenAI-compatible 或 Anthropic 路线。凭证只填写环境变量名称。`deepseek-flash` 的文本/工具路线使用 `thinking: "disabled"`。模型 ID、协议与思考选项按实际服务和所选路线配置。请求费用、模型 token 上限与外部 LoopX 分钟配额是不同量，不能互相替代。

## 任务与上下文输入

首次任务在创建会话前准备。普通 CLI 和 SDK `KernelCat.runTask({task,onEvent,signal})` 使用 `task.json`，其中 `goal` 是交给 Pi 的 prompt。托管 SDK 使用完整的 `run.json` manifest；选用 Goal 模式后绑定 `LoopXGoal`，执行仍由 Pi 持有。`run()` 执行已选择的输入，任务文字来自这些文件。可直接修改和运行的 CANN Vector/Cube task、SDK runner 与独立验收检查见 [CANN 示例](../examples/cann/README.md)。

### 独立任务 JSON

把下列内容保存为 `/absolute/task.json`，按自己的算子修改说明、约束和候选源码，再交给上文的 `run --task`：

```json
{
  "id": "vector-add-001",
  "scope": "customer-vector-add",
  "goal": "优化逐元素 Float32 向量加法。保持输入不变，先检查正确性，再按配置评估器比较性能。参考说明：长度包含零和不规则尾部。",
  "contract": {
    "operator": "vector_add",
    "dtype": "float32",
    "maximum_length": 131072,
    "correctness": "exact rounded sum; inputs preserved"
  },
  "seed_files": {
    "solution.js": "export function vectorAdd(a, b) { const out = new Float32Array(a.length); for (let i = 0; i < a.length; i++) out[i] = Math.fround(a[i] + b[i]); return out; }\n"
  }
}
```

`id` 标识原任务，`scope` 标识任务范围说明，`goal` 是本次请求与说明，`contract` 是显式约束，`seed_files` 将候选文件写入获准工作区。`scope` 本身不授予目录权限；实际可写范围由受信任配置与工具准入决定。独立评估器和参考实现放在受保护配置中。该示例说明任务输入格式，评估器的调用合同仍由配置决定。

### 托管 SDK 的任务字段

| 输入内容 | `run.json` 字段 |
| --- | --- |
| 目标、解释性上下文和参考摘录 | `goal` |
| 算子语义、shape/dtype、硬约束、目标平台和基线身份 | `contract_source_text`：完整 TaskContract 的原始 JSON 文本 |
| 父程序与可编辑候选文件 | `program.entrypoint`、`program.configuration_source`、`program.files` |
| 本轮改变什么、预期结果和停止条件 | `proposal` |
| 模型路由与额度 | `model_route`、`run_credit_limit`、`trial_credit_limit` |
| 账户与恢复身份 | `account_id`、各 Run/Trial/Branch/Session ID 和稳定 `intent_id` |

`proposal` 是保留的实验计划记录；需要模型直接看到的解释和参考摘录写入 `goal`。`program.files` 会写入工作区，上下文列出文件路径与摘要，正文由获准工具读取；在 `goal` 中明确需要阅读的文件。

根 README 的 SDK 示例会生成可检查的 `sdk/manifest.json`。自定义任务时，在创建新 Run 前准备自己的完整 manifest；父程序字节须与 TaskContract 的 baseline 摘要一致，其他必填字段按权威 schema 提供。

### 追加初始参考文本

对于一小段说明，可以在首次启动前由调用方读取文件并放入新任务的 `goal`。下面是准备输入的片段：`run-draft.json` 必须已经是该新 Run 的完整 manifest 草稿，尚未提交或冻结。

```js
import { readFile, writeFile } from "node:fs/promises";

const task = "优化向量加法，保持数学语义并用配置评估器验证";
const context = await readFile("/absolute/input/notes.md", "utf8");
const manifest = JSON.parse(await readFile("/absolute/input/run-draft.json", "utf8"));
manifest.goal = `${task}\n\n参考说明：\n${context}`;
await writeFile("/absolute/input/run.json", JSON.stringify(manifest, null, 2), { flag: "wx" });
```

文本成为本次显式任务输入并参与原始输入绑定。大份资料接入 `knowledge_root` 后按问题读取；候选源码、参考实现和评估器分别放在其获准位置。运行开始后沿用原 manifest 和身份，追加说明通过下文的会话输入接口提交。

## 切换模型与配置输入

`examples/vector-add/provider-configs.json` 提供 DeepSeek、OpenAI 与 Anthropic 的 `providers.primary` 字段模板。先按根 README 生成 `--prepare-only` 输入，将模板中对应对象写入生成的 `config.json` 的 `providers.primary`，然后在本机设置其 `api_key_env` 对应环境变量，再启动 SDK。不要改写 `python_modules`、评估器摘要或已经冻结的 manifest；修改已启动运行的配置会触发身份校验。

```sh
# 使用本机安全的凭证注入方式设置 OPENAI_API_KEY 后：
python3 - <<'PYCONFIG'
import json
from pathlib import Path
bundle = Path('/absolute/KernelBridge')
config = Path('/absolute/prepared-vector-add/config.json')
value = json.loads(config.read_text())
value['providers']['primary'] = json.loads((bundle / 'examples/vector-add/provider-configs.json').read_text())['openai']
config.write_text(json.dumps(value, indent=2))
PYCONFIG
node examples/vector-add/sdk-run.mjs --bundle /absolute/KernelBridge \
  --input /absolute/prepared-vector-add --loopx /absolute/KernelBridge/runtime/venv/bin/loopx
```

模板中的模型 ID 必须是该服务和账户实际可用的型号。服务地址、协议、模型及凭证环境变量应在首次启动前一起确定；模型列表、鉴权成功和一次任务完成分别核对。服务不可达、鉴权拒绝、限流、模型不可用和未确认结果保留原始错误及回执，不盲目重试。

## 生成、评估与产物

候选源码保存在任务工作区，执行身份、已准入操作及回执保留在配置的 state/journal/artifact 存储中。候选材料、正确性判断、性能观测和最终采用各有自己的记录。读取评估结果时核对候选内容、父程序、checker、环境与测量协议；只有这些身份对应的实际回执才能支持本次结论。

普通独立任务可用于生成程序。启用 `rsi` 后，受保护配置还需给出源码文件、策略、固定评估器命令与摘要、计费及超时；有限适配器按合同返回分解、成对测量和独立确认。没有匹配评估器时不要把生成结果写成正确性或速度结果。公开榜单提交始终是另行授权的效果。

`--timeout-ms` 默认 300000，控制 CLI 本次进程总时限。评估器、模型服务和任务自身还可能有各自上限；延长 CLI 时限不会增加 credits、设备权限或模型预算。中断与进程退出也不能证明远端效果已经取消。

## 创建会话与运行任务（TypeScript SDK）

需要在同一账户下控制 Run/Trial 时，先显式创建账户，再提供完整 Run manifest：

```sh
./kernelagent account-create --config /absolute/config.yaml \
  --intent customer-account-001 --credits 200 --multi-trial
./kernelagent run --config /absolute/config.yaml --manifest /absolute/run.json
./kernelagent runtime-state --config /absolute/config.yaml \
  --run-id ORIGINAL_RUN_ID --trial-id ORIGINAL_TRIAL_ID
```

账户回执中的身份必须写入 manifest；manifest 还包含精确任务合同、程序内容和运行限制，不能用独立 `--task` 文件代替。权威结构见 `source/contracts/execution/v1/runtime.schema.json` 与 `runtime_control.schema.json`。`--multi-trial` 创建新 v3 账户，不转换旧账户；Run 和 Trial 共享原账户的累计使用记录。托管结果分别记录生成状态与独立评估资格；验证和测量结论按本次配置评估器的原始回执判断。`run --manifest` 只执行一次，不会隐式创建或绑定 LoopX Goal。操作人准备下一 Trial 使用明确的 `PrepareTrialRequest`，这是手动的一步，不会重置预算：

```sh
./kernelagent run --config /absolute/config.yaml \
  --manifest /absolute/run.json --next-trial /absolute/prepare-trial.json
```

已准备好的独立向量加法示例也可通过 SDK 运行。在根 README 的 `--prepare-only` 步骤生成输入后执行：

```sh
node examples/vector-add/sdk-run.mjs \
  --bundle /absolute/KernelBridge \
  --input /absolute/prepared-vector-add \
  --loopx /absolute/KernelBridge/runtime/venv/bin/loopx
```

完整示例把冻结的任务目标绑定到原始 LoopX Goal，通过原始有限 CLI 创建账户，调用 `KernelCat.createSession()`、`runStreamed()`、`state()` 和 `dispose()`，由 `LoopXGoal.advance()` 准入、验证并结算有限的一轮。它保留原始任务目标、八次模型请求上限、每次 2048 输出 token 及原有 RSI 评估器；在输入目录下新建 `sdk/` 保存账户意图、冻结的合同与 manifest、有序事件、结果和最终状态。相同目录不能再次启动，以免重发结果不明的效果。该示例使用本包的源码和锁定运行时；没有复制另一套 SDK。

SDK 来自本包的 `source/apps/agent`。下面采用安装后可直接解析的源码入口：把代码保存为客户数据目录中的 `run-sdk.mjs`，替换绝对路径，并使用安装选定的 Node 执行。原账户与 manifest 须已准备好。

```ts
import { KernelCat } from "/absolute/KernelBridge/source/apps/agent/src/sdk/index.ts";

const client = new KernelCat({
  python: "/absolute/KernelBridge/runtime/venv/bin/python",
  config: "/absolute/config.yaml",
  expectedPythonModules: "/absolute/KernelBridge/runtime/expected.json",
  timeoutMs: 300000,
  maxFrameBytes: 1048576,
});
const session = await client.createSession({ manifest: "/absolute/run.json" });
try {
  const result = await session.run();
  console.log(result.status);
  console.log(result.runtime_state);
} finally {
  await session.dispose();
}
```

```sh
/absolute/node /absolute/my-task/run-sdk.mjs
```

该命令会执行任务并调用配置的模型。SDK 源码入口使用本包已经安装的锁定依赖。

上例使用默认安装路径，指定自定义 runtime 时替换 Python 和模块绑定路径。构造和选择会话不调用模型；`run()` 或 `runStreamed()` 才进入执行。事件流提供有序生命周期与实际会话事件，当前模型传输返回完整响应，**不提供 token 流**。`state()` 读取原始运行权威；`interrupt()` 取消本 handle 拥有的进程；`dispose()` 可重复调用。

### 读取执行事件

在上述 session 的 `try` 中，用以下片段替换 `session.run()` 即可读取有序事件，再取得最终结果；两种方式每次选一种。

```js
const invocation = session.runStreamed();
for await (const event of invocation.events) {
  console.log(event.sequence, event.type, event.payload);
}
const result = await invocation.result;
console.log(result.runtime_state);
```

事件包括 `ready`、`harness`、`runtime`、`input`、`completed` 和 `failed`。它们是生命周期与执行观察；`harness` 记录会话实际观察。

### 在执行中补充说明

`ready` 之后、该次调用仍活跃时，可以提交 `prompt`（追加问题）、`steer`（调整方向）或 `followUp`（后续说明）。它们接受相同生成合同，`kind` 须与方法对应。以下片段放在活跃会话的控制逻辑中：

```js
const state = await session.state();
const receipt = await session.steer({
  intent_id: "customer-guidance-001",
  expected: state.stamp,
  kind: "steer",
  text: "补充说明：保留输入数组，优先检查尾部路径，不放宽正确性要求。",
});
console.log(receipt);
```

`expected` 来自本次会话当前状态；若状态已变化，会得到显式冲突。回执表示说明已入队，不表示模型已经消费，也不授予新的效果权限。保留原意图和正文处理丢失确认；原 Task 硬约束继续冻结。

创建/选择会话、`run`、事件读取、输入与恢复均使用现有 SDK。会话输入的具体初始参数以 manifest 为准；执行中使用上述带状态版本的输入请求。

## 中断与恢复

先读取原始状态和回执，再决定继续。已完成操作复用原回执；UNKNOWN 或丢失确认的操作保留身份与预留，禁止换新 ID 重发。配置了固定 `collect_argv` 的 RSI 适配器可收取原已准入作业：

```sh
./kernelagent rsi-collect --config /absolute/config.yaml \
  --task /absolute/task.json --action-id ORIGINAL_ACTION_ID
```

这个命令不提交新实验，也不补造缺失的会话 checkpoint。托管活动会话的继续需要稳定的 `ReplaceSessionRequest`：

```sh
./kernelagent run --config /absolute/config.yaml \
  --manifest /absolute/run.json --resume /absolute/replace-session.json
```

同一恢复请求也可从 SDK 提交。以下片段复用前面配置好的 `client`；新进程按同一 Worker 配置创建 `client`，再选择原 manifest：

```js
const restored = await client.selectSession({ manifest: "/absolute/run.json" });
try {
  const result = await restored.run({ resume: "/absolute/replace-session.json" });
  console.log(result.runtime_state);
} finally {
  await restored.dispose();
}
```

`resume` 接受稳定请求文件的绝对路径，也可传入完整的 `ReplaceSessionRequest` 对象。准备下一 Trial 时，以同样方式调用 `run({ nextTrial: "/absolute/prepare-trial.json" })`。文件和对象使用同一验证路径，分别保留原有意图、父程序与执行版本；不以应用会话 ID 代替恢复合同。

丢失回复后保留同一请求文件及意图身份，通过 `runtime-state` 与原始回执对账。未决效果仍会阻止新效果；恢复不会自动清空预算或预留。上下文组织、知识检索及只读历史会话分析见 [KernelBridge](../kernelbridge/README.md)。

## 按工作流运行与判断结果

一次完整目标工作流从 LoopX Goal 开始。Goal 保留目标、当前 Todo 与配额；SDK 把一次受准入的执行落到原始 Run/Trial。执行时读取当前源码和回执，诊断后提出假设与实验，候选接受只依据独立评估。只读 `kernel-reviewer` 子 agent 可以审计当前源码，但其意见不能代替正确性检查和测量。知识与当前状态更新保存在原始 Journal；下一轮继续使用原 Goal，并提交带当前版本的下一 Trial 请求。

本例的 LoopX 入口运行一个有界 Turn。`progress` 表示该 Turn 的候选、审计与测量通过独立验证且已结算，Goal 仍保留；它不是“任务已经完成”。`step_limit` 表示一次 Trial 的八步上限；`no_progress`、`validation_failed`、`recovery_required` 分别需要诊断、补证或原身份对账。出现未决效果时停止后续准入，保留原始预留。

自动跨 Trial 的公共入口和默认下一步策略见下节。示例的 `sdk/` 额外保存账户意图、`manifest.json`、冻结合同、事件、最终状态、LoopX Goal/Turn 读回与验证脚本。不要对同一个准备目录再次运行创建入口。继续时保留原 manifest、Goal、Run 和账户，先对账后提交明确的新意图。

## LoopX 自动续接入口

使用 `pursue --goal` 绑定已经由 owner 配置好的 Goal、Agent、Todo 和分钟配额。它不会在预检时注册 Agent、规划 Todo、创建 Goal 或补充配额。首次 Run 仍须有已创建账户和合法 manifest。历史 `loopx-cycle` 与 SDK 的 Goal 绑定互斥，不要把两个驱动叠在同一 Goal 或执行段上。

把下面内容保存为 `/absolute/pursuit.json`，将所有路径和身份替换为本次已有资源。JSON 不接受注释或额外字段；权威定义为 `source/contracts/execution/v1/runtime.schema.json` 的 `GoalPursuitBinding`。`validator_argv` 是独立校验器的明确 argv，不是 shell 命令串。

```json
{
  "schema_version": "kernelcat.goal-pursuit/v1",
  "worker": {
    "python": "/absolute/KernelBridge/runtime/venv/bin/python",
    "config": "/absolute/config.yaml",
    "expected_python_modules": "/absolute/KernelBridge/runtime/expected.json",
    "timeout_ms": 300000
  },
  "session": {
    "manifest": "/absolute/run.json"
  },
  "loopx": {
    "cli": "/absolute/KernelBridge/runtime/venv/bin/loopx",
    "node": "/absolute/node",
    "project": "/absolute/goal-project",
    "registry": "/absolute/goal-project/registry.json",
    "runtime_root": "/absolute/goal-runtime",
    "home": "/absolute/goal-home",
    "timeout_ms": 60000
  },
  "goal": {
    "goal_id": "customer-goal",
    "agent_id": "customer-agent",
    "todo_id": "todo_existing",
    "validator_argv": ["/absolute/node", "/absolute/protected/validate.mjs"],
    "validator_timeout_seconds": 30
  },
  "pursuit": {
    "intent_id": "customer-pursuit-001",
    "max_turns": 5,
    "trial_credit_limit": 30
  }
}
```

`session.application_session_id` 可显式指定，否则按账户与 Run 派生；它与原生会话 ID 分离。`pursuit.resume` 和 `pursuit.next_trial` 可分别提供完整的 `ReplaceSessionRequest` 或 `PrepareTrialRequest` 对象，二选一，用于这次调用的第一步；后续步骤由默认 planner 提议。每个请求带原始意图与精确执行版本。`trial_credit_limit` 是后续 Trial 的上限，仍受原账户、Run 的剩余额度约束。

从交付根目录运行：

```sh
./kernelagent pursue --goal /absolute/pursuit.json
```

同一个绑定文件也可从 SDK 调用，执行和清理路径与 CLI 相同。将以下内容保存为 `.mjs` 文件，替换绝对路径后用安装选定的 Node 运行：

```ts
import { pursueGoal } from "/absolute/KernelBridge/source/apps/agent/src/sdk/index.ts";

const result = await pursueGoal("/absolute/pursuit.json");
console.log(result);
```

`pursue` 内部只由 `LoopXGoal` 循环：每步重新检查 Goal 状态、配额、待处理输入和原始 Journal 的预算及未决效果，上一 Trial 结算并销毁原会话 后才可能准入下一 Trial。相同已提交意图只读重放；同意图改输入会报冲突。崩溃留下的推进 claim 不自动抢占，须按原 Turn 对账。

默认 `createStageRouterNext(session, { manifest, intentId, trialCreditLimit })` 读取当前权威状态后调用 `stageRouter`。它只为 `explore`、`establish_incumbent`、`confirm` 构造明确的下一 Trial，父程序必须已保留在图中。`reconcile`、`stop`、`wait` 仅产出停止建议，LoopX 用当前 fingerprint 条件写入原始生命周期记录并保留原因。`round-control` 提供诊断与验收证据，无权另起循环。

**当前默认会等待。** 托管状态缺少权威剩余时间和候选 Evaluation 观测，字段保持 `unknown`。首次有效进展后，默认 planner 因时间未知返回 `wait`，LoopX 记录原因并返回 `paused`，不会再创建 Trial 或请求模型。`max_turns` 只是本次调用边界；到达上限返回 `invocation_limit`，不表示 Goal 完成。

需要自己产生下一 Trial 的 SDK 调用方仍可传 `LoopXGoal.pursue({ first, maxTurns, next })`；`next` 返回明确的 `GoalTurnRequest`，或 `{ stop: { reason } }`／`undefined` 表示不再提议。所有准入仍由 LoopX 执行，调用方不能以自写循环绕开 Goal，也不能把未观测的条件当作已满足。

暂停调用 `goal.pause((await goal.read()).stateFingerprint, reason)`；恢复先用新的 `read()` fingerprint 调用 `goal.resume(fingerprint, reason)`，再显式提交新的 pursuit 意图及第一步请求。未解决未知时间观测时，默认 planner 仍会等待。`clear(fingerprint)` 停止并解除应用附着，保留原始证据与用量。生命周期冲突或结果不明时先读回原 owner 状态，不盲目重试，不删除 claim 或释放预留。

历史会话支持 [一条命令恢复](../examples/cann/README.md#一条命令恢复原会话)：校验归档后，以 Pi SessionManager 还原原 session ID、当前分支与完整上下文，输出 `session.html`、`session.jsonl` 和原生 Pi 打开命令。Sigmoid 的重新实验命令与历史会话恢复命令分别列在 [复现指南](../examples/cann/sigmoid-optimization/README.md)。
