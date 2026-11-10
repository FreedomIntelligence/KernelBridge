# KernelBridge

默认使用 Pi SDK 执行模型与基础工具回合；启用 Goal 时由 LoopX 管理目标与调度。KernelBridge 负责权限、隔离、预算、原始凭据与会话恢复。NPU 评测、知识库、IR、profiler 作为可配置业务工具接入。

## 一键还原

Linux/macOS 准备 Python 3.11+，在解压目录运行：

```sh
bash restore.sh --run /absolute/delivery/runs/sdk-durable-final-vector-add \
  --output /absolute/restored/vector
```

脚本验证交付文件、选择兼容 Node、安装锁定 Python/Pi/LoopX 依赖、验证归档哈希，恢复完整 Pi 会话及日志。运行环境位于 `.restore/`；`--cache` 可以指定其他目录。`--python`、`--node` 可选择现有解释器；默认 Node 下载包使用官方 SHA256 校验。`--install-only` 只完成环境安装。

在目标设备继续执行：

```sh
bash restore.sh --run /absolute/delivery/runs/sdk-durable-final-vector-add \
  --output /absolute/restored/vector-next \
  --profile /absolute/profiles/npu.json \
  --npu-python /absolute/npu-venv/bin/python --continue
```

续跑保留历史对话，以已验收候选为起点，创建新的执行身份、预算和 LoopX Goal。`lineage.json` 关联新旧记录。`--prepare-only` 生成冻结输入；`--goal` 指定新的优化目标。

## 算子任务与原生代码

[Vector Add、Cube Matmul](examples/cann/README.md) 包含可编辑 task、测试数据、参考、Ascend C `.cpp/.h` 和 six-source wrapper。[Sigmoid](examples/cann/sigmoid-optimization/README.md) 包含原始起点、最终实现、20 用例以及 40-turn 的历史记录。

```sh
node examples/cann/run.mjs --task /absolute/tasks/vector-add/task.json \
  --profile /absolute/profiles/npu.json --output /absolute/runs/vector-goal
```

CANN 示例默认进入 Goal 模式；`--mode generate` 使用普通 Pi SDK 会话。通用 SDK 用法见 [KernelCat](kernelcat/README.md)，上下文与知识库见 [KernelBridge](kernelbridge/README.md)。

## 指令与工具

通用 [AGENTS.md](examples/cann/instructions/AGENTS.md) 与任务附录由准备器冻结，Pi `DefaultResourceLoader.agentsFilesOverride` 将两份文档放入 system prompt 的 `project_context`。它们随任务记录哈希并设为只读。

Pi 提供 `read/write/edit/bash/grep/find/ls`。部署 profile 选择评测与知识工具；`diagnostics: true` 提供 IR/profiler 采集、解析、HTML 展示和分页读取。其他设备通过 `config.business_tools` 注册命令、参数合同、哈希、预算与超时。详见 [通用算子工具](examples/cann/tools/README.md)。

`npu_python` 指定设备侧已有 Torch/torch_npu 的环境；`npu_environment` 配置 CANN 路径与动态库。主机侧 Python 包、Pi 1.5.0、LoopX 1.2.3 由锁文件对齐。Linux 的 Pi bash 隔离使用 bubblewrap，macOS 使用 Seatbelt。

## 独立安装与开发

```sh
python3 -I -B install.py --verify-only
python3 -I -B install.py --python /absolute/python3 --node /absolute/node --npm /absolute/npm
runtime/venv/bin/python -I -m pip install --no-deps --require-hashes -r source/apps/agent/loopx-requirements.txt
./kernelagent --help
```

回归：

```sh
npm run check:types --prefix source/apps/agent
node --test validation/*.mjs
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s validation -p 'test_*.py' -v
python3 -I -B install.py --verify-only
```

## 交付内容

完整 ZIP 含 `KernelBridge/` 源码与脚本、`runs/` 四组原始会话、`logs/` 编译与评测日志，以及逐文件哈希清单。每次新运行保留 SDK 事件、KernelBridge 账本、Pi session/checkpoint、候选、诊断、评测、验收和 manifest。

[githubgist.diff](githubgist.diff) 是相对初始提交 `87c8fe4` 的源码补丁；补丁自身、派生清单以及已移除的部署专用接入文件单独记录。当前文件身份以 `PIN_MANIFEST.json` 为准。

验证记录见 [CANN 交付检查](validation/CANN_DELIVERY.md) 和 [原始设备验收](validation/ACCEPTANCE_RESULTS.md)。
