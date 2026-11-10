# CANN 算子：可编辑任务、原生 C++ 与 Pi SDK + LoopX Goal

默认流程是 **task → LoopX Goal → Pi SDK 会话 → NPU 评测与修复 → 验收和日志归档**。提供两个可修改的初始化算子：**Vector Add FP32 `[128]`**、**Cube Matmul FP16 `[128,128]`（FP32 累加）**。示例验证环境为 Ascend910_9382、逻辑 NPU 1、CANN 9.1、torch_npu 2.10；示例包含五个随机种子的正确性检查与完整 Pi SDK 执行流程。另有 [Sigmoid 优化结果](sigmoid-optimization/README.md)。验收要求见 [验收说明](../../validation/ACCEPTANCE.md)。

## 文件分工

```text
examples/cann/
├── run.mjs                         SDK 提交入口，也导出 runCannTask / prepareCannTask
├── profiles/npu.example.json       可信模型、权限和预算配置，无密钥
├── vector-add/ / cube-matmul/
│   ├── task.json                   编辑 goal；引用 case 与 baseline
│   ├── case.json                   shape、dtype、分布、五个种子、容差、目标设备
│   ├── reference.py                根据 case 生成的正确答案与输入
│   ├── baseline.py                 完整 six-source 初始候选
│   ├── task.compiled.json          传给底层 KernelCat.runTask 的冻结格式
│   └── native/
│       ├── *_custom.json          算子工程定义
│       ├── op_host/*.cpp          注册、形状与 host tiling
│       ├── op_kernel/*.cpp        Ascend C Vector / Cube 内核
│       ├── op_kernel/*_tiling.h    host/device tiling 布局
│       ├── bindings/op.cpp        Torch NPU 扩展绑定
│       ├── bindings/model.py      ModelNew 接口
│       └── sources.json           六个字段的路径与原始哈希
├── pack_native.py                 原生文件 -> six-source 封装，不执行源代码
├── evaluate.py                    已配置 CANN 主机上的业务评测入口
├── prepare_goal.py / goal.mjs      既有托管 SDK + LoopX 入口
└── export.mjs                     原始 session、Pi HTML 与逐文件哈希导出
```

**`.py` 不是用 Python 实现 CANN 计算。** 评测协议把 C++ 源码放在六个字符串中，由编译器生成 CANN 工程、编译 `.so` 并在 NPU 执行。`native/` 提供相同源码的独立文件，便于阅读、修改和交付；测试核对六个字段逐字一致。完整 CMake 工程由已锁定的编译器/设备工具链生成，不能直接对单个 `.cpp` 使用宿主 `g++` 编译。

## 修改与提交

1. 复制对应算子目录；修改 `task.json` 的 `goal` 和 `case.json`。仅支持当前两个语义、standard_normal 分布与五个不同种子。改 shape、dtype、SoC 或 CANN 版本后重新评测，不继承旧凭据。
2. 若修改原生实现，编辑 `native/` 后重新封装；如果只修改需求以让 Agent 生成，不必改 baseline。

```sh
python3 -B examples/cann/pack_native.py \
  --native /absolute/tasks/vector-add/native \
  --output /absolute/tasks/vector-add/baseline.py
```

维护仓库示例时，`make_tasks.py` 更新 `task.compiled.json`、参考和请求，保留手写 goal。自定义目录由 `run.mjs` 自动编译，避免旧参考与新 case 不一致。`sources.json` 的哈希是交付初始源码身份，修改后不再代表已验证版本。

3. 按[根 README](../../README.md)安装，将 `profiles/npu.example.json` 复制到可信配置目录，填写 Node、Python、expected.json；配置可用模型服务及模型 ID，凭据只放在 `api_key_env` 指定的环境变量。默认评测适配器在**当前已配置的 NPU 主机**执行，安装后还需 Torch/torch_npu/CANN 和设备权限。远程部署通过 `config.business_tools` 提供 `baseline_check`、`operator_check`、`operator_evaluate`；性能模式配置持久化 RSI 提交/查询适配器。单会话模式使用 `config.evaluator`。

```sh
node examples/cann/run.mjs \
  --task /absolute/tasks/vector-add/task.json \
  --profile /absolute/profiles/npu.json \
  --output /absolute/runs/vector-add-001 --prepare-only
# 核对准备结果后，用另一个新目录实际执行：
node examples/cann/run.mjs \
  --task /absolute/tasks/vector-add/task.json \
  --profile /absolute/profiles/npu.json \
  --output /absolute/runs/vector-add-002
```

Cube 换成对应 task 即可。每次新尝试使用新目录；有未决效果时查原记录，禁止为了重试而重新提交。`--prepare-only` 不启动模型或设备。可从应用直接调用同一示例入口：

```js
import { runCannTask } from './examples/cann/run.mjs';
await runCannTask({ task: '/absolute/task.json', profile: '/absolute/npu.json',
  output: '/absolute/runs/new-run' });
```

它先冻结 task、baseline、TaskContract 和评测配置，再调用现有 `KernelCat.createSession()` 与 `LoopXGoal`，`task.goal` 进入 Pi 初始 prompt。`--mode generate` 是 Vector/Cube 默认模式，从占位候选开始生成并由 LoopX 验收；`--session-only` 使用 `KernelCat.runTask()` 执行单次会话。底层 SDK 也可直接接受 `task.compiled.json`；编辑用 `task.json` 是本示例的输入格式，不直接传给底层 API。旧五参数 `--config/--python/--expected-python-modules` 调用兼容保留，使用已编译 task。

## Goal 模式与架构

Pi 负责模型与工具回合；基础 read/write/edit/bash/grep/find/ls 使用 Pi 原生能力。NPU 评测、Knowledge、原始凭据作为业务扩展。KernelBridge 强制权限、隔离、预算和凭据；LoopX 是可选 Goal 实现，负责目标、调度、验证与结算。

默认 `mode: 'generate'` 从任务生成实现，使用 `KernelCat.createSession()` + `LoopXGoal.pursue()`。`--mode optimize` 从 baseline 开始性能优化，Sigmoid 历史复现默认使用该模式。随包 profile 提供 LoopX 路径；准备器自动生成匹配 case 的 TaskContract、NPU 工具配置和源码摘要。功能 Goal 注册基线、快速检查和最终评测工具；性能 Goal 使用成对测量。远程部署可提供自己的 `config.rsi` 与 `task_contract_file`。Vector/Cube 使用五种固定输入和独立计时样本，Sigmoid 使用原 20 用例协议。执行循环统一由 Pi 持有。

默认 `goal.max_trials: 3`，`complete_on_acceptance: true`：独立验收通过即完成 Goal。设为 false 时按预算继续生成明确的下一 Trial；达到 max_trials 记录 invocation_limit。一次 Pi Trial 默认最多运行 40 个模型回合；NPU 评测次数由实际工具调用决定。功能与性能任务分别采用声明的验收条件，流程见 [开发工作流](../../validation/CODEGEN_WORKFLOW.md)，测量方式见 [性能示例](../../validation/PERFORMANCE_EXAMPLES.md)。

## 结果与完整日志

新运行包含冻结 `task.json`、`config.redacted.json`、`inputs/`、SDK `events.jsonl`、KernelBridge `state/events.jsonl`、`state/pi-sessions/` 原始会话和 checkpoint、`workspace/`、`evaluations/`、`result.json`、`acceptance.json` 与逐文件 `manifest.json`。普通 SDK 的实际候选在 `workspace/<task-digest>/solution.py`。导出根据验收摘要或 Goal materialization 引用生成 `candidate/solution.py` 与 `candidate/identity.json`，并在顶层 workspace 尚无同名文件时增加同一候选的便捷副本。原始工作区草稿保持原样。Goal 原始 SDK 结果在 `sdk/`，导出时增加顶层索引，保留所有原路径。

```sh
node examples/cann/export.mjs \
  --input /absolute/runs/completed-run \
  --output /absolute/archive/new-export
```

导出拒绝符号链接、重复输出目录及凭据形态内容；使用已锁定 Pi 自带导出器生成原生 session HTML，不重新调用模型或 NPU。原始绝对 locator 保留用于溯源；移动后的阅读通过 manifest 相对路径进行，新环境通过根目录 restore.sh 重新绑定本机运行依赖。

完整交付包顶层 `runs/` 包含Vector/Cube 功能开发、Cube 性能优化及 Sigmoid 优化的可恢复会话；`logs/` 额外保留编译、正确性、计时及工具执行日志。设备二进制留在原设备，交付的是二进制哈希和构建凭据。只有独立 `acceptance.accepted=true` 或原始 LoopX validator passed 才表示相应验收通过。

## 一条命令恢复原会话

在 Linux/macOS 上运行根目录脚本。脚本校验源码、配置 Node、安装锁定 Python/Pi/LoopX 依赖，然后校验归档并恢复完整对话：

```sh
bash restore.sh --run /absolute/delivery/runs/sdk-durable-final-vector-add \
  --output /absolute/restored/vector
```

恢复目录保留原始 JSONL、checkpoint、SDK 事件、执行账本、评测凭据和 HTML。`restore.json` 记录 session ID、leaf、上下文摘要及归档身份。

在目标设备继续生成、评测和修复：

```sh
bash restore.sh --run /absolute/delivery/runs/sdk-durable-final-vector-add \
  --output /absolute/restored/vector-next \
  --profile /absolute/profiles/npu.json --npu-python /absolute/npu-venv/bin/python \
  --continue --mode optimize --goal "继续优化完整算子延迟，保持任务精度与输入覆盖"
```

`--prepare-only` 生成续跑输入供检查。续跑以已验收的最终候选为起点，保留原 Pi 会话历史，并创建新的 KernelBridge 执行身份、预算和 LoopX Goal；`lineage.json` 关联原会话与新运行。新的会话副本更新 cwd，原归档保持原始字节。

## AGENTS 与可选诊断工具

Pi `DefaultResourceLoader` 通过 `agentsFilesOverride` 加载任务中的 `AGENTS.md` 和 `AGENTS.task.md`，随后由 Pi 构建 system prompt 的 `project_context`。通用文件来自 [算子优化规则](instructions/AGENTS.md)，任务附录包含当前 goal 和工具用法；准备器冻结文本、记录 SHA256，并纳入只读路径。

profile 的 `diagnostics: true` 暴露采集、解析、展示工具，见[通用工具说明](tools/README.md)。其他平台通过 `config.business_tools` 提供自己的命令和参数合同。`npu_python` 选择已有 Torch/torch_npu 的解释器，`npu_environment` 显式补充 CANN 的 PATH、库路径和 Python 路径。普通生成、RSI 和诊断采集从同一冻结配置恢复这些变量。

## 设备检查与验收

`baseline_check()` 编译并验证冻结初始实现，成功后开放候选修改与评测。`operator_check()` 运行编译和五种输入的正确性检查。`operator_evaluate()` 完成最终检查与 100 次 NPU event 计时，返回源码、参考及构建身份。功能 Goal 按编译与正确性验收，计时用于记录；性能 Goal 由 RSI 的成对确认决定是否接受改进。

工具执行前，KernelBridge 冻结候选字节；工具返回的源码哈希必须匹配这份冻结内容。LoopX 独立 validator 再核对最终 Program、原始评测凭据和未决效果，随后提交 Todo 完成和 Goal acceptance。
