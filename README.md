# KernelBridge

KernelBridge 默认提供当前项目中的交互式 codegen。Pi SDK 执行模型与文件、终端工具；KernelBridge 保存会话和执行回执。Goal、Trial 和 NPU 验收由用户显式开启，LoopX 负责 Goal 调度。

## 普通 codegen

安装后在项目目录运行 `kernelagent`（或本仓库 `./kernelagent`）：先显示输入框，输入任务后执行，完成后等待下一条输入。不会自动发送演示任务、创建 Trial 或运行设备验收。

```sh
./kernelagent
./kernelagent -C /absolute/project --config /absolute/model-config.json
./kernelagent --model YOUR_MODEL "检查并修复这个项目的构建错误"
./kernelagent --continue
./kernelagent resume APPLICATION_ID
```

模型设置读取显式 `--config`、`KERNELBRIDGE_CONFIG` 或 `~/.kernelbridge/config.json`；格式见 [配置示例](examples/codegen/config.example.json)。密钥使用配置指定的环境变量。未配置模型时仍可进入界面查看帮助和加载配置。普通工具直接操作选定项目，shell 使用本地环境中的工具；隔离副本只用于显式试验模式。

DeepSeek 官方配置见 [DeepSeek 示例](examples/codegen/deepseek.example.json)：`ds` 使用 `deepseek-flash`，`ds-pro` 使用 `deepseek-v4-pro`；`/model ds-pro` 或启动时 `--model ds-pro` 可切换。凭据可放在环境变量 `DEEPSEEK_API_KEY`，或用户目录 `~/.kernelbridge/credentials.json` 的同名字段（文件归当前用户所有，权限 0600）。环境变量优先；凭据不会写入项目配置、会话快照或源码清单。当前默认使用非思考模式，以完整保留原生工具回合的兼容性。端点与模型名依据 [DeepSeek 官方文档](https://api-docs.deepseek.com/en/)。

已配置模型时，启动直接进入 Pi 1.5.0 的完整 `InteractiveMode`，保留原生 logo、主题、编辑器、补全、滚动和快捷键。宿主会话可在启动时建立，但空闲状态不会请求模型、修改项目文件、创建 Goal/Trial 或执行验收。模型未配置时也使用原生 Pi 界面提供设置入口。

**不设置预算即无额度限制。** `credits`、`policy.max_steps` 省略或设为 `null` 时没有对应上限；状态栏不显示预算。需要限制时才使用 `--budget 200` 或配置中的正整数 `credits`。Goal 的模型回合、Trial 和 fork 上限也只在显式配置时生效。账本仍记录实际调用与消耗，但不会据此建立隐含额度。

`/new` 或 `/clear` 开始新对话并保留工作文件；`/model` 查看/切换配置中的模型路由；`/compact` 压缩上下文；Esc 中断；`/quit` 退出。`/goal 目标内容` 显式开启持续执行，`/goal pause|resume|clear` 管理该目标。

## 目录

```text
examples/cann/       Vector、Cube、Sigmoid 任务与 Ascend C 源码
kernelcat/          SDK、会话与 Goal 接口说明
kernelbridge/       上下文、知识库与业务工具说明
source/             Agent、Worker、Executor、Knowledge、Ingestion
validation/         验收要求、使用指南与自检工具
restore.sh          环境配置与会话还原入口
install.py          锁定依赖安装与源码校验
PIN_MANIFEST.json   交付源码文件清单与 SHA256
```

完整交付 ZIP 另含 `runs/` 算子开发会话、`logs/` 编译和评测输出，以及交付级 `manifest.json`。

## 一键还原

Linux/macOS 准备 Python 3.11+，在源码目录执行：

```sh
bash restore.sh --run /absolute/delivery/runs/cube-matmul-goal-final \
  --output /absolute/restored/cube
```

脚本校验源码和归档，选择兼容 Node，安装锁定 Python、Pi 和 LoopX 依赖，恢复原生 Pi session、checkpoint、SDK 事件与执行凭据。`restore.json` 记录会话与上下文身份。

`--python`、`--node` 选择已有解释器；`--cache` 设置安装缓存；`--install-only` 只安装环境。设备编译与执行所需的 CANN、驱动、Torch/torch_npu 和权限由 NPU profile 指定。

## 提交算子任务

原生 Pi TUI 与常驻会话入口见 [TUI 和持久 SDK 会话](docs/tui.md)：

```sh
./kernelagent tui --task /absolute/tasks/vector-add/task.json \
  --profile /absolute/profiles/npu.json --output /absolute/runs/vector-tui
./kernelagent sessions
./kernelagent tui --attach APPLICATION_ID
```

显式 task 默认也不启动持续 Goal；添加 `--goal-from-task` 或进入后使用 `/goal 目标内容` 才启用。活跃 Goal 默认退出后暂停；显式 `--on-detach continue` 允许后台继续。真实 NPU 资格与本地功能验收分开记录。

[Vector Add 与 Cube Matmul](examples/cann/README.md) 提供可编辑的 goal、测试输入、参考、初始实现和独立 Ascend C `.cpp/.h` 文件。[Sigmoid](examples/cann/sigmoid-optimization/README.md) 提供性能优化示例。

```sh
node examples/cann/run.mjs --task /absolute/tasks/vector-add/task.json \
  --profile /absolute/profiles/npu.json --output /absolute/runs/vector
```

Vector/Cube 默认创建功能生成 Goal：验证 baseline，再生成、编译、检查正确性、修复和验收。`--mode optimize` 选择性能优化，`--session-only` 使用单次 Pi SDK 会话，`--prepare-only` 只生成冻结输入。

profile 的 `goal.max_trials` 控制 Trial 数量，`policy.max_steps` 控制每个 Trial 的模型回合数。`goal.complete_on_acceptance: true` 在独立验收通过后完成 Goal。

## 指令与扩展工具

准备器冻结通用 [AGENTS.md](examples/cann/instructions/AGENTS.md) 和任务附录，Pi `DefaultResourceLoader.agentsFilesOverride` 将其加载到模型上下文。

Pi 提供 `read/write/edit/bash/grep/find/ls`。KernelBridge 通过权限和账本管理工具执行；NPU 评测、Knowledge 与诊断工具由 profile 选择。`diagnostics: true` 启用 IR/profiler 采集、解析、展示和读取；其他平台通过 `config.business_tools` 注册命令与参数合同。详见 [诊断工具](examples/cann/tools/README.md)。

`npu_python` 选择设备解释器，`npu_environment` 配置 CANN 路径和动态库。Linux shell 隔离使用 bubblewrap，macOS 使用 Seatbelt。

## 查看结果与继续执行

`acceptance.json` 与 `result.json` 记录结果；`candidate/solution.py` 是通过凭据绑定的源码，`candidate/identity.json` 记录其身份。`manifest.json` 索引原生 session、日志和评测文件。

```sh
bash restore.sh --run /absolute/delivery/runs/cube-matmul-goal-final \
  --output /absolute/restored/cube-next \
  --profile /absolute/profiles/npu.json --continue --mode optimize \
  --goal "优化矩阵乘延迟，保持任务输入与精度"
```

续跑保留历史对话和已验收源码，创建新的执行身份、预算与 Goal；`lineage.json` 关联原会话。

## 独立安装与开发

```sh
python3 -I -B install.py --verify-only
python3 -I -B install.py --python /absolute/python3 --node /absolute/node --npm /absolute/npm
runtime/venv/bin/python -I -m pip install --no-deps --require-hashes -r source/apps/agent/loopx-requirements.txt
./kernelagent --help
```

已有锁定依赖的源码开发目录可复用解释器与当前 npm 树，不必再复制旧 bundle：

```sh
/absolute/installed/python -B source/scripts/develop.py
./kernelagent
```

该命令检查 `requirements.lock`，构建并校验当前 Python 源码的冻结 wheel，写入本仓库 `.development/`。不会安装全局依赖或改变被复用的解释器；修改 Python 源码后重新运行。开发入口始终是本仓库 `./kernelagent`。

无需真实模型即可手动体验普通 codegen：`python3 -B validation/tui/demo.py`。这是本地固定脚本演示，先等待输入；旧 bundle 中已经打开的终端不会随源码更新，请退出后从本仓库重新启动。自动化终端检查与复现命令见 [TUI 验证说明](validation/tui/README.md)。

接口和验证说明：

- [SDK 与 Goal](kernelcat/README.md)
- [上下文与知识库](kernelbridge/README.md)
- [算子开发流程](validation/CODEGEN_WORKFLOW.md)
- [交付包使用](validation/DELIVERY_GUIDE.md)
- [验收要求](validation/ACCEPTANCE.md)
- [功能自检](validation/VALIDATION_GUIDE.md)
- [性能示例](validation/PERFORMANCE_EXAMPLES.md)
