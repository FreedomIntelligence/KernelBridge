# KernelBridge

KernelBridge 提供通用算子开发工作流：默认由 Pi SDK 执行模型与基础工具回合，LoopX 管理 Goal 和任务调度，KernelBridge 管理权限、隔离、预算、源码与执行凭据。编译评测、知识库、IR 和 profiler 作为可配置业务工具接入。

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

接口和验证说明：

- [SDK 与 Goal](kernelcat/README.md)
- [上下文与知识库](kernelbridge/README.md)
- [算子开发流程](validation/CODEGEN_WORKFLOW.md)
- [交付包使用](validation/DELIVERY_GUIDE.md)
- [验收要求](validation/ACCEPTANCE.md)
- [功能自检](validation/VALIDATION_GUIDE.md)
- [性能示例](validation/PERFORMANCE_EXAMPLES.md)
