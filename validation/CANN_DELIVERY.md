# CANN task / Goal 交付检查

本次交付将 task 准备接到原有 managed SDK + LoopX：默认冻结 baseline、测试、TaskContract 和配对评测配置，建立 Goal，由同一 Pi 会话执行工具循环。`--mode generate` 进入普通 `KernelCat.runTask()`。

| 检查 | 结果 |
| --- | --- |
| Python 回归 | 45 passed |
| Node 回归 | 21 passed |
| TypeScript 与 managed continuation 检查 | passed |
| Vector / Cube / Sigmoid 的 Goal 准备 | 三组通过，TaskContract 经原校验器校验 |
| 三组 RSI 配置的摘要与权限边界 | 原 check_configuration 通过 |
| 三组实际 RSI 分解入口 | 通过，生成一个源码变更单元 |
| 三个算子的独立原生文件与 six-source wrapper | 六字段逐字一致 |
| 日志导出 | 四组完整会话，原生 Pi HTML，逐文件 SHA256 |
| 旧 CANN 验收条件 | 最终源码绑定、五种输入、NPU 计时与未决效果回归通过 |

原在线/NPU实验记录对应 `80c1c0b`：Vector/Cube 生成、Cube 编译修复，以及 Sigmoid 40-turn Goal。新统一入口本轮完成离线准备、协议和回归检查。完整交付包 `logs/delivery-checks/` 保存这次测试输出；`logs/` 的三个归档保存之前全部成功和失败日志。Sigmoid 的复现命令执行同一实验流程，并生成新的测量记录。
