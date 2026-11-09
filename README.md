# KernelBridge 源码交付

KernelAgent 面向通用算子生成：把算子语义、输入约束、目标平台和预算交给 Agent，得到候选程序与对应执行记录。KernelCat 是产品 CLI 和应用 SDK；KernelBridge 提供上下文组织、知识与原始证据检索、只读历史会话分析，复用同一 KernelCat 会话、有限 Python Worker 和原始执行账本。

首次使用按本页完成安装和独立向量加法示例。日常任务、SDK 和恢复见 [KernelCat](kernelcat/README.md)；上下文、知识与只读会话回放见 [KernelBridge](kernelbridge/README.md)。使用本产品无需选择某个 benchmark，也无需提交公开榜单。

## 交付内容与环境

```text
KernelBridge/
├── README.md
├── install.py / requirements.lock / PIN_MANIFEST.json
├── kernelagent
├── kernelcat/README.md
├── kernelbridge/README.md
├── examples/vector-add/
└── source/
    ├── apps/agent/              TypeScript CLI、SDK 与会话执行
    ├── apps/worker/             有限 Python 服务与组件装配
    ├── packages/               Executor、Knowledge、Ingestion
    └── contracts/              协议与配置的权威 schema
```

本安装流程使用 Linux/macOS 的 POSIX 路径。准备 Python 3.11 或更新版本、Node.js 22.19.0 或更新版本、npm 和联网环境。安装程序会创建独立 Python 虚拟环境。源码和 Node 完整依赖锁随包交付；Python 主机侧依赖与构建工具的固定版本记录在 `requirements.lock`。完整 Goal 工作流使用 LoopX 1.2.3，需要 Node 22.22.3 或更新版本。普通 `run` 只执行一次；自动跨 Trial 的产品入口为 `pursue --goal`，其绑定文件与 SDK 用法见 [KernelCat](kernelcat/README.md#loopx-自动续接入口)。

首次示例只需要 CPU。运行真实设备算子还需单独准备相应编译器、驱动、运行时和设备访问权限，并配置匹配的独立评估器。主机侧安装不包含 Torch、CANN 或设备环境。交付包不含模型凭证、开发机虚拟环境、历史会话或客户运行数据。

## 在线安装

解压到固定位置。以下 `/absolute/...` 均需替换成本机绝对路径；安装后保持该目录及选定解释器位置有效。

```sh
cd /absolute/KernelBridge
python3 -I -B install.py --verify-only
python3 -I -B install.py \
  --python /absolute/python3 \
  --node /absolute/node \
  --npm /absolute/npm
./kernelagent --help
# 完整 Goal 工作流的固定控制平面依赖
runtime/venv/bin/python -I -m pip install --no-deps --require-hashes \
  -r source/apps/agent/loopx-requirements.txt
```

安装联网获取锁定依赖，默认在 `runtime/` 创建运行环境；可用 `--runtime /absolute/new-runtime` 选择新目录。安装使用本包源码构建四个 Python wheel，校验实际安装内容后生成 `runtime/expected.json`。`.installation/` 保存本机安装绑定，启动器沿用安装时选定的 Node、Python 和模块绑定。启动器不安装依赖。

`PIN_MANIFEST.json` 记录交付源码版本与文件摘要，安装和启动会检查对应内容。安装失败时保留原目录及日志用于诊断，重新解压到新目录再安装；不要覆盖原安装或修改摘要绕过检查。安装日志、运行环境及客户数据均不属于源码交付内容。

## 第一个独立算子：向量加法

示例位于 `examples/vector-add/`，使用 CPU 上的 JavaScript 向量加法演示任务、父程序、模型配置和有限评估器的接线。输入为等长 `Float32Array`，元素是 [-1000,1000] 内的有限数，长度覆盖零与不规则尾部，最大 131072；输出须等于逐元素 `Math.fround(a[i] + b[i])`，且保留两份输入。先在新数据目录做离线检查，不需要模型凭证：

```sh
python3 -I -B examples/vector-add/run.py \
  --bundle /absolute/KernelBridge \
  --output /absolute/runs/vector-add-offline \
  --offline-check
```

离线检查将分配较多中间数组的教学父程序与随包手写候选分别送入真实正确性检查、交错成对计时及独立确认。它检查示例评估器，不是模型生成结果。每个计时样本包含八次调用；纳秒/调用包含 VM 调用与分配开销，准备和正确性检查不计入其中。JIT、GC 与系统噪声会影响数值。需要先检查生成的任务和配置时，使用另一个新目录并加 `--prepare-only`，该模式不启动评估器或模型。

准备模型服务后，运行由 LoopX 持有 Goal 的完整示例。默认模型 ID 为 `deepseek-flash`，服务地址为 `https://api.deepseek.com/v1`，凭证从当前环境的 `DEEPSEEK_API_KEY` 读取。先在本机配置该环境变量，再运行；不要把密钥写入任务或配置文件。

```sh
python3 -I -B examples/vector-add/run.py \
  --bundle /absolute/KernelBridge \
  --output /absolute/runs/vector-add-live \
  --model deepseek-flash --prepare-only
node examples/vector-add/sdk-run.mjs \
  --bundle /absolute/KernelBridge \
  --input /absolute/runs/vector-add-live \
  --loopx /absolute/KernelBridge/runtime/venv/bin/loopx
```

此命令会调用所配置的模型服务，可能产生费用。每个有限 Trial 限制八个模型步骤、每次最多 2048 输出 tokens，Run 共用 180 credits；达到一次 Trial 的上限会保留状态，后续 Trial 需再次经过 LoopX 的准入、验证、配额和 Goal 状态检查。上限不保证完成次数或性能改善。示例读取本机安装绑定，生成配置与任务，通过实际 SDK 执行托管 Run/Trial，再由原始 LoopX 独立验证与结算；无需手写占位路径或接入榜单。这个示例调用一次 `LoopXGoal.advance()`，不是自动多轮入口。`pursue` 的默认路由当前因缺少权威剩余时间观测而在首次有效进展后建议等待，LoopX 记录原因并暂停 Goal；不会直接假定还有时间继续试验。

| 本次输出目录内的文件 | 用途 |
| --- | --- |
| `config.json`、`task.json`、`command.json` | 本机配置、算子任务与精确产品调用 |
| `protected/adapter.mjs`、`protected/runtime.json` | 固定评估器与运行时信息，位于可写候选目录之外 |
| `workspace/solution.js` | 独立任务的候选源码；托管 SDK 的当前工作区以其 `runtime_state` 为准 |
| `state/events.jsonl`、`state/rsi-*` | 真实运行的原始 Journal 与 RSI 证据 |
| `stdout.log`、`stderr.log`、`result.json` | 直接任务入口运行时的输出、诊断与结构化结果 |
| `sdk/manifest.json`、`sdk/events.jsonl`、`sdk/result.json`、`sdk/state.json` | 上述 SDK 命令的实际输入、有序事件、结果与最终状态 |
| `offline-check.json` | 离线模式的正确性、原始成对计时和独立样本身份 |

每次新尝试使用不存在的输出目录。失败或 UNKNOWN 时保留原目录，先检查原始回执与未决操作，不要为绕过故障换目录重发同一效果。

生成成功、正确性通过和性能改善是三个不同结果。查看本次输出和原始评估回执，确认候选源码、正确性状态、计时范围与环境身份。未测量的结果保留 `unknown`；进程退出或离线检查通过不代表真实设备验收。

## 数据位置与后续使用

`source/` 保存产品实现，客户任务、工作区、journals 与新产物放在独立数据目录。模型配置、可写范围与预算由客户明确指定；自动跨 Trial 必须显式配置 LoopX；单次执行允许独立 KernelCat 会话。Knowledge、RSI 评估与 Replay 按各自功能开关配置，关闭 Replay 不关闭 LoopX 的目标与配额管理。

后续自定义算子可从示例生成的任务与配置出发，更换语义、输入范围、父程序和独立评估器。保留原始账本、回执、模块绑定和未决操作身份；升级源码不能通过清空预留、改写历史摘要或复制旧状态来强行继续。具体命令见 [KernelCat](kernelcat/README.md)，可选能力见 [KernelBridge](kernelbridge/README.md)。
