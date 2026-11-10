# 性能优化示例

性能模式从完整初始实现出发，由 Pi 修改源码、读取诊断并调用成对评测工具；LoopX 管理目标与预算。

## Cube Matmul

FP16 [128,128] 矩阵乘，以 FP32 累加并输出 FP16。示例使用 20 个主模型回合完成候选提出、编译检查、修复、成对测量和确认，保留一个通过验收的优化实现。独立确认的平均延迟为 baseline 32.951 µs、candidate 25.049 µs，平均延迟比约 1.32×。测量使用相同设备、五种输入和固定精度合同。

完整会话位于交付包 runs/cube-20turn-accepted/，包含候选、原始样本、编译日志以及 IR/profiler 记录。

```sh
node examples/cann/run.mjs \
  --task /absolute/tasks/cube-matmul/task.json \
  --profile /absolute/profiles/npu.json \
  --output /absolute/runs/cube-optimization --mode optimize
```

## Sigmoid

Sigmoid 示例提供 20 个用例，覆盖 FP16/BF16/FP32、不规则长度、NaN/Inf 与输入保持。40 回合结果采用 40 核、2048 元素分块和双缓冲。独立本地成对测量相对随包单核初始实现为 16.91×，相对 Torch 为 0.512×；逐例数据在 examples/cann/sigmoid-optimization/performance.json。

执行说明、完整源码与测试数据见 [Sigmoid 示例](../examples/cann/sigmoid-optimization/README.md)。

## 读取性能结果

按任务合同、源码摘要、编译身份、输入覆盖和原始样本关联测量。修改任务或环境后重新执行成对评测；复现运行会生成新的计时和验收文件。
