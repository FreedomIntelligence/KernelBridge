# KernelBridge 交付入口

KernelBridge 以 Pi SDK 执行算子开发会话，LoopX 管理 Goal 与任务调度。文件权限、进程隔离、预算、源码身份和执行凭据由 KernelBridge 管理；编译评测、知识库、IR 和 profiler 作为业务工具接入。

## 1. 交付目录

```text
README.md           统一使用入口
KernelBridge/       源码、任务示例、安装与还原脚本
runs/               已验收的完整算子开发会话
logs/               编译、正确性、计时与工具执行输出
manifest.json       交付文件清单、版本与 SHA256
```

提供 Vector Add、Cube Matmul 和 Sigmoid 示例。Vector/Cube 各含 task.json、case.json、reference.py、baseline.py 和独立 Ascend C 源文件。

## 2. 查看或还原完整会话

Linux/macOS 准备 Python 3.11+，在解压目录执行：

```sh
bash KernelBridge/restore.sh \
  --run "$PWD/runs/cube-matmul-goal-final" \
  --output /absolute/restored/cube
```

脚本校验源码与归档，选择兼容 Node，安装锁定 Python、Pi 和 LoopX 依赖，还原原生 session、checkpoint、SDK 事件和执行凭据。打开还原目录的 `session.html` 查看完整对话；`restore.json` 记录 session ID、leaf、上下文摘要和文件校验结果。

`--python`、`--node` 选择已有解释器，`--cache` 指定安装缓存。会话还原使用本地归档，不调用模型或设备。

## 3. 安装任务运行环境

在源码目录安装；Node 使用 22.22.3 或更新的兼容版本：

```sh
cd KernelBridge
python3 -I -B install.py \
  --python /absolute/python3 \
  --node /absolute/node \
  --npm /absolute/npm
runtime/venv/bin/python -I -m pip install --no-deps --require-hashes \
  -r source/apps/agent/loopx-requirements.txt
```

复制 `examples/cann/profiles/npu.example.json` 到部署配置目录，填写模型、Node、Python 和设备环境。模型凭据放入 `api_key_env` 指定的环境变量。`npu_python` 选择已安装 Torch/torch_npu 的解释器，`npu_environment` 配置 CANN 的 PATH、动态库和 Python 路径。远程设备通过部署提供的业务工具适配器接入。

## 4. 编写 task 并提交

复制 `examples/cann/vector-add/` 或 `examples/cann/cube-matmul/`，修改 task.json 的 goal 与 case.json 的 shape、种子、容差和设备。Vector 示例使用 FP32，Cube 示例使用 FP16 输入输出与 FP32 累加。修改原生实现时编辑 native/，然后用 pack_native.py 生成 baseline.py。

```sh
node examples/cann/run.mjs \
  --task /absolute/tasks/vector-add/task.json \
  --profile /absolute/profiles/npu.json \
  --output /absolute/runs/vector
```

默认执行：**提交 task → 创建 LoopX Goal → Pi 读取规则与源码 → 验证 baseline → 生成、编译、检查、修复 → 独立验收**。

| 选项 | 用途 |
| --- | --- |
| `--mode generate` | 功能生成，Vector/Cube 的默认模式 |
| `--mode optimize` | 从初始实现开始性能优化 |
| `--session-only` | 使用单次 Pi SDK 会话 |
| `--prepare-only` | 只生成冻结任务与配置 |
| `goal.max_trials` | profile 中的 Trial 数量上限 |
| `policy.max_steps` | profile 中每个 Trial 的模型回合上限 |
| `goal.complete_on_acceptance` | 独立验收通过后完成 Goal |

应用可以通过同一 SDK 提交入口调用：

```js
import { runCannTask } from './examples/cann/run.mjs';

const result = await runCannTask({
  task: '/absolute/tasks/vector-add/task.json',
  profile: '/absolute/profiles/npu.json',
  output: '/absolute/runs/vector',
});
```

每次新运行使用新的输出目录。Pi 原生基础工具与业务工具在同一会话中工作，AGENTS.md 和任务附录由 SDK 资源加载器放入模型上下文。

## 5. 验收与日志

先查看 `acceptance.json` 和 `result.json`。`completed` 表示 Goal acceptance 已通过；`candidate/solution.py` 保存通过凭据绑定的源码，`candidate/identity.json` 记录其 SHA256。`manifest.json` 索引 session、账本、评测与诊断文件。

功能流程使用 baseline_check、operator_check、operator_evaluate；性能流程使用成对测量与独立确认。IR/profiler 通过 profile 的 diagnostics 选项启用。

导出运行：

```sh
node examples/cann/export.mjs \
  --input /absolute/runs/vector \
  --output /absolute/archive/vector
```

## 6. 在已有会话上继续

在解压目录执行：

```sh
bash KernelBridge/restore.sh \
  --run "$PWD/runs/cube-matmul-goal-final" \
  --output /absolute/restored/cube-next \
  --profile /absolute/profiles/npu.json --continue --mode optimize \
  --goal "优化矩阵乘延迟，保持任务输入与精度"
```

续跑保留完整对话和已验收源码，以新的执行身份、预算与 Goal 执行。`lineage.json` 关联原归档。增加 `--prepare-only` 可先检查续跑输入。

## 7. 详细说明

- [开发流程](CODEGEN_WORKFLOW.md)
- [验收要求](ACCEPTANCE.md)
- [自检与协议测试](VALIDATION_GUIDE.md)
- [性能优化示例](PERFORMANCE_EXAMPLES.md)
- [Vector / Cube 任务与原生源码](../examples/cann/README.md)
- [SDK 与 Goal API](../kernelcat/README.md)
- [上下文与知识库](../kernelbridge/README.md)
