# KernelBridge

KernelBridge 是通用代码与算子开发 Agent。交互界面使用 Pi；会话、工具执行和恢复由 KernelBridge 管理。启动后等待任务输入。Goal、算子评测和 NPU 工具按需开启。

## 安装

支持 Linux 和 macOS，需要 Python 3.11+、Node 22.19+（含 Zstd 支持，建议 22 LTS）和 npm。在新检出的目录执行：

```sh
python3 -I -B install.py --verify-only
python3 -I -B install.py \
  --python /absolute/path/to/python3 \
  --node /absolute/path/to/node \
  --npm /absolute/path/to/npm
runtime/venv/bin/python -I -B -m pip install --no-deps --require-hashes \
  -r source/apps/agent/loopx-requirements.txt
./kernelagent --help
```

安装器校验源码清单，安装锁定依赖，构建 Python 运行环境并记录安装位置。首次安装需要联网；安装失败后请使用新的检出目录重试。也可用 `bash restore.sh --install-only` 自动选择兼容的 Node 环境。

## 模型与启动

将 [配置模板](examples/codegen/config.example.json) 复制到 `~/.kernelbridge/config.json`，填写端点、模型和密钥环境变量名。DeepSeek 可使用 [DeepSeek 模板](examples/codegen/deepseek.example.json)。密钥放在对应环境变量中，不写入项目。

```sh
./kernelagent
./kernelagent -C /absolute/project --config /absolute/model-config.json
./kernelagent --continue
./kernelagent resume
```

`/help` 查看命令，`/model` 切换模型，`/resume` 恢复会话，`/fork` 分支会话，`/compact` 压缩上下文，`/agents` 查看子代理，`/trace` 打开执行轨迹。Esc 中断当前执行，`/quit` 退出。

`/goal 目标内容` 开启持续执行；`/goal pause|resume|clear` 管理目标。不设置预算时没有总 token、调用次数或额度上限；需要限制时显式使用 `--budget` 或配置相应预算。

## 算子与 NPU

运行工具位于 `examples/`：`operator_pipeline/` 提供任务固定、候选冻结、评测、ABBA 配对、诊断和晋升；`cann/tools/` 提供 IR 与 profiler；`scatter/` 提供 CPU 和 NPU 驱动。`cann/` 中的任务目录提供可修改的输入和原生源码模板。

```sh
./kernelagent tui --task /absolute/task.json \
  --profile /absolute/npu-profile.json --output /absolute/new-run
```

设备地址、解释器、CANN、Torch/torch_npu 和权限由 profile 显式配置。使用 [NPU profile 模板](examples/cann/profiles/npu.example.json)；`diagnostics: true` 开启诊断工具。任务不会隐式启动 Goal，可添加 `--goal-from-task`。未提供性能 baseline 时先建立正确性通过的候选，再与该候选比较。

## 还原会话

```sh
bash restore.sh --run /absolute/archive/run --output /absolute/new-restored-run
```

还原器验证归档与源码身份。加 `--profile /absolute/npu-profile.json --continue --mode optimize` 可继续算子会话。设备执行所需环境仍由 profile 提供。
