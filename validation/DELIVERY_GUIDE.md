# 交付包使用指南

## 目录

```text
KernelBridge/       源码、依赖锁文件、安装与还原脚本
runs/               可恢复的算子开发会话
logs/               编译、正确性、计时与验证输出
githubgist.diff     相对初始版本的源码补丁
manifest.json       文件大小、SHA256 与交付版本
```

源码目录包含 Vector Add、Cube Matmul 和 Sigmoid 示例。每个示例提供任务、测试数据、参考、初始实现以及独立 Ascend C 源文件。

## 安装与会话还原

Linux/macOS 准备 Python 3.11+。在解压目录执行：

```sh
bash KernelBridge/restore.sh \
  --run "$PWD/runs/cube-matmul-goal-final" \
  --output /absolute/restored/cube
```

脚本校验源码与归档，配置兼容 Node，安装锁定 Python、Pi 和 LoopX 依赖，恢复原始 Pi session、checkpoint、事件与凭据。restore.json 记录 session ID、leaf、上下文摘要与归档身份。

指定已有解释器可使用 --python、--node；--cache 选择安装缓存目录；--install-only 只安装环境。设备端的 CANN、驱动、Torch/torch_npu 和权限通过部署 profile 配置。

## 提交与续跑

```sh
node KernelBridge/examples/cann/run.mjs \
  --task /absolute/tasks/vector-add/task.json \
  --profile /absolute/profiles/npu.json \
  --output /absolute/runs/vector

bash KernelBridge/restore.sh \
  --run "$PWD/runs/cube-matmul-goal-final" \
  --output /absolute/restored/cube-next \
  --profile /absolute/profiles/npu.json --continue --mode optimize \
  --goal "优化矩阵乘延迟，保持任务输入与精度"
```

新运行使用独立的输出目录。续跑保留历史对话和已验收源码，以新的执行身份、预算与 Goal 执行；lineage.json 关联两次运行。

## 结果阅读

先读取 acceptance.json 与 result.json，再按 candidate/identity.json 查看已验收源码。manifest.json 提供各类日志与 session 的相对路径。原生 JSONL 和对应 HTML 展示完整模型与工具交换；设备日志提供编译、正确性和计时明细。

验收要求见 [验收说明](ACCEPTANCE.md)，自检命令见 [验证指南](VALIDATION_GUIDE.md)。
