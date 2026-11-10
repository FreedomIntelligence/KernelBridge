# CANN task / Goal 交付检查

本次交付将 task 准备接到原有 managed SDK + LoopX：默认冻结 baseline、测试、TaskContract 和配对评测配置，建立 Goal，由同一 Pi 会话执行工具循环。`--mode generate` 进入普通 `KernelCat.runTask()`。

| 检查 | 结果 |
| --- | --- |
| Python 回归 | 51 passed |
| Node 回归 | 25 passed |
| TypeScript 与 managed continuation 检查 | passed |
| Vector / Cube / Sigmoid 的 Goal 准备 | 三组通过，TaskContract 经原校验器校验 |
| 三组 RSI 配置的摘要与权限边界 | 原 check_configuration 通过 |
| 三组实际 RSI 分解入口 | 通过，生成一个源码变更单元 |
| 三个算子的独立原生文件与 six-source wrapper | 六字段逐字一致 |
| 原始会话 one-shot 恢复 | 四组全部通过；Pi session ID、leaf ID、完整上下文一致 |
| 日志导出 | 四组完整会话，原生 Pi HTML，逐文件 SHA256 |
| 旧 CANN 验收条件 | 最终源码绑定、五种输入、NPU 计时与未决效果回归通过 |

原在线/NPU实验记录对应 `80c1c0b`：Vector/Cube 生成、Cube 编译修复，以及 Sigmoid 40-turn Goal。新统一入口本轮完成离线准备、协议和回归检查。完整交付包 `logs/delivery-checks/` 保存这次测试输出；`logs/` 的三个归档保存之前全部成功和失败日志。Sigmoid 的复现命令执行同一实验流程，并生成新的测量记录。

## 跨环境与通用工具回归

- 根目录 `restore.sh` 在新安装目录安装锁定依赖，恢复 Vector 原会话并生成新的 Goal 输入；路径包含空格。
- 四组历史会话逐文件哈希、原 session ID、leaf 和上下文核对通过。
- 真实 Pi/Gateway 在新工作区导入历史会话后继续执行编辑和评测，保持参考只读。
- 通用 AGENTS 与任务附录进入真实模型上下文；只读保护通过。
- 通用 business_tools 注册、Pi 调用、LLVM 解析、分页读取、HTML 展示及哈希校验通过。
- 正式 CommandEvaluator 的最小环境保留指定虚拟环境、CANN 路径与输入快照身份。
- Vector、Cube、Sigmoid 的新 Goal 配置通过安装后 Worker 和 TaskContract 校验。

本轮设备诊断单独记录在完整交付包 `logs/portable-restore/`。

NPU 诊断采集：Ascend910_9382、逻辑设备 1 上完整编译、五种输入正确性与 profiler Level1/PipeUtilization 采集通过；原始 timeline、CSV、源码/二进制身份和编译日志随包归档。

IR 采集通过完整原生编译，重放留存的设备编译命令生成 bitcode，再解码为 LLVM 文本。归档包含原始命令、导出命令、编译器身份、源码哈希、bitcode 与 `llvm/0-kernel.ll`。CANN 环境的 `CMAKE_PREFIX_PATH` 与工具链变量在最小执行环境中保留。

安装还原验证使用 macOS arm64；设备诊断使用 Linux aarch64、CANN 9.1、Python 3.12、Torch/torch_npu 2.10。两次全新目录安装分别还原 Vector 和 Sigmoid，并准备带原始对话的新 Goal。
