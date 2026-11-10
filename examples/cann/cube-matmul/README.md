# Cube Matmul

初始化语义：FP16 `[128,128]` × FP16 `[128,128]`，FP32 累加、FP16 输出。实现采用 Ascend C Matmul API，单 Cube 核，不修改输入，不使用 Torch 代算候选。测试为 standard_normal 分布，种子 1024–1028；具体容差、设备与形状以 [case.json](case.json) 为准。

直接阅读 [native/](native/) 下的 `.cpp` 与 `.h`。`baseline.py` 是相同六个源码字段的评测封装；`reference.py` 仅用于生成输入和正确答案。此前验证平台是 CANN 9.1 / Ascend910_9382 / torch_npu 2.10 / 逻辑 NPU 1。其他尺寸、平台需要重新验证。

修改目标用 [task.json](task.json)；修改数据用 [case.json](case.json)；修改实现用 `native/`，随后运行：

```sh
python3 -B examples/cann/pack_native.py --native /absolute/tasks/cube-matmul/native --output /absolute/tasks/cube-matmul/baseline.py
```

完整 SDK 提交、profile 配置与日志说明见 [CANN 示例入口](../README.md)。默认提交创建 LoopX Goal，从 baseline 开始进行生成、评测与修复；显式 `--mode generate` 从占位候选开始执行单次 Pi 会话。
