# 通用算子诊断工具

Pi 负责模型与工具回合，LoopX 提供可选 Goal 调度。工具通过 KernelBridge 的 `business_tools` 注册，配置明确命令、参数合同、源码哈希、预算与超时。CPU、GPU、NPU 采集器均可使用这个接口；`examples/cann` 提供 Ascend 的配置示例。

| 类别 | Pi 工具 | 产物 |
| --- | --- | --- |
| 采集 | `ir_collect` | 当前候选的设备 LLVM、编译日志、源码与编译器身份 |
| 采集 | `profiler_collect` | 正确性检查、NPU timeline、PipeUtilization、原始 CSV |
| 解析 | `diagnostics_parse` | LLVM 函数与调用统计、profiler CSV 字段及记录 |
| 展示 | `diagnostics_render` | 复用 PyPTO 的 LLVM 对照 HTML |
| 读取 | `diagnostics_read` | collection 文件清单、哈希、分页文本 |

在 CANN profile 中设置 `"diagnostics": true` 即可暴露这些工具。`false` 或省略该字段使用当前任务配置的基础工具与评测工具。`config.business_tools` 接入其他平台的命令适配器；适配器从 stdin 读取 Action，stdout 返回 `{"status":"completed","data":{...}}`，原始标准输出、错误输出和 Outcome 写入执行账本对应目录。

采集输出位于 `evaluations/diagnostics/<collection>/`；工具调用回执位于 `state/business-tools/<tool>/`。解析与展示引用采集返回的 collection 和相对路径，按清单哈希读取。采集使用独立构建目录，性能验收使用任务配置的配对计时协议。

`ir_bridge.py`、`trace_lab.py` 与 `vendor/pypto` 复用现有 IR/trace 展示代码；上游版本及逐文件哈希在 `vendor/pypto/provenance.json`，许可证随目录保留。LLVM 展示比较显式选定的文本快照。Trace 清洗保留原始采集，按 core 和 PC 对照指令统计。

部署采集器配置本机编译器、驱动、CANN 和 `torch_npu`。远端部署可配置直接 SSH 的 `host`、`user`、`port` 与 `identity`，或使用自有的业务工具适配器。

## 与任务流程衔接

基线验证通过后，Pi 可根据编译错误或性能问题选择采集工具。采集结果带源码和构建身份；解析、展示与读取工具使用 collection 内的相对路径。设备命令由 profile 配置，模型凭据通过环境变量注入。
