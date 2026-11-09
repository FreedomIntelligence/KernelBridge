# Sigmoid：40 回合原生优化产物

`solution.py` 是实际通过 Pi SDK + LoopX 运行保留下来的六段源码，未作离线改写；SHA256 和来源见 `provenance.json`。起点为人工写出的保守单核实现（`validation/fixtures/sigmoid_baseline.py`），先验证正确，再交给 DeepSeek Flash 做 40 个父模型回合的优化。不是从零生成成功的声明。

最终保留 40 核、每块 2048 元素和双缓冲流水；失败的更大分块候选未被采用。使用固定 CANNBench 提交的 Season 1 Sigmoid 全部 20 用例，覆盖 FP16/BF16/FP32、非整块长度、NaN/Inf；完整输出按原 MERE/MARE 阈值检查，另查输入未修改。新种子的独立确认通过，LoopX 独立 validator 也通过。

该文件须交给受信任的 six-source CANN 执行器编译，不能直接作为 Python CPU 算子运行。当前实测平台是 Ascend910_9382 / CANN 9.1 / torch_npu 2.10；完整用例适配器为 `validation/cannbench_sigmoid_adapter.py`，需要另行准备冻结的原始测试集与数据生成器。性能数据、失败尝试和边界见 [验收记录](../../../validation/ACCEPTANCE_RESULTS.md)。这是本地实验，没有提交 CANNBench 平台或产生官方分数。

最终独立复测：相对保守单核起点 16.91×；与 Torch 的两组交换顺序配对合并为 0.512×，即整体仍慢约 1.95 倍。完整 20 用例均正确。摘要见 `performance.json`，不宣称超过 Torch。
