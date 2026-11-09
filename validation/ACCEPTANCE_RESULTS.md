# Pi、SDK、LoopX 与 NPU 验收记录

2026-11-09。本记录对应同一提交中的源码和测试，导入基线为 `87c8fe4`。原始运行记录保存在本机 `acceptance-full` 证据目录；失败、被中断的运行和新运行分别保留，不合并为一次成功实验。性能实验结果和归档索引在本记录末尾单独列出。

## 产品入口

- `KernelCat.runTask({ task, onEvent })` 接收 task 文件，其中 `goal` 是传入 Pi 的初始 prompt，`contract` 和只读 seed 文件补充任务约束。它复用现有 Pi SDK 会话、Gateway、工具权限与凭据。
- 托管模式使用 `KernelCat.createSession({ manifest })`；选择 goal 模式时绑定 `LoopXGoal`，由 LoopX 管理目标、预算和验收。模型仍由同一个 Pi 循环执行。
- Pi 默认提供 read/write/edit/bash/grep/find/ls。NPU evaluator、Knowledge、原始结果凭据是业务工具。普通 evaluator 与托管 RSI adapter 有不同的准入协议；示例明确区分，不能把普通 task JSON 当作 Run manifest。
- 新增 `examples/cann/run.mjs`、`goal.mjs` 与 Vector/Cube task 示例。没有新增另一套模型执行循环。

## 原五项问题

| 问题 | 当前固定回归 |
| --- | --- |
| 参考答案被候选输入修改污染 | 独立输入存储图、冻结参考输出、非法输入修改检查；CPU 和真实 NPU 对照验证 |
| 普通任务 reference 可写 | 普通任务、RSI、Pi 文件工具、旧工具及 shell 写回统一受可写路径约束 |
| Knowledge raw grep 符号链接越界 | 与 read 一样验证实际路径；库外文件不进入搜索结果 |
| 无效工具调用留下不能恢复的回合 | 确定未执行的调用留下失败凭据和可恢复检查点 |
| MCP 固定 300 秒截断 | 默认业务工具直连；可选 MCP 超时与 Worker 配置对齐 |

这些回归限制在声明的场景；并不构成对任意生成代码或所有硬件的绝对保证。

## 设备与在线测试新增修复

1. CANN 9.1 生成项目布局兼容：支持两种 tiling 头位置和现代 `npu_op_kernel_options` API；安装到作业独立 OPP 目录；保留 CANN 的 PYTHONPATH。
2. 编译、加载和精度失败分别报告。源码 digest、安装后的二进制 SHA256、完整编译 stdout/stderr 与计时原始样本进入作业证据。
3. NPU 输入存储克隆兼容 torch_npu 的 Byte TypedStorage；避免对 NPU tensor 图使用 deepcopy。整型/布尔输出精确比较，检查 dtype、非有限值位置、共享视图和初始化输入。
4. 显式选择 NPU 设备和 stream。实测发现设置 `ASCEND_DEVICE_ID=1` 并不能切换 torch_npu 的默认设备；修复前产生的性能数字不用于加速结论，已通过的正确性证据仍单独保留。
5. 可信 six-source Request 支持 atol/rtol，拒绝布尔、负数、非有限值和字符串。默认仍为 1e-4，候选不能修改阈值。Cube 示例使用 0.01。
6. 模型 usage 贯通 Gateway、Pi 与子调用；分别处理 provider 缓存 token 口径。未配置计价的 cost=0 不表示免费或实测账单。
7. Node 启动除版本外检查 Zstd API，并实际导入 Pi。Node 23.7 缺少所需 API，原来会在子进程内退出；现在提前拒绝，且 Pi host 保留错误 stderr。
8. Linux bash 使用可信 bubblewrap 路径、最小环境、私有 tmp 和空 `/proc`；无法挂载 proc 的嵌套环境不再回退暴露宿主 proc。依赖 `/proc` 的命令不在此沙箱支持范围。
9. RSI 在创建 proposal 前校验 KU 引用、确认预算及源码变更；虚构引用、已耗尽预算或尚未编辑源码均返回可恢复的工具失败，不留下待接受的坏提案。
10. RSI 向 Pi 返回 discovery 的精度、性能和诊断摘要。confirmation 原始数据仍保持私有。编译摘要优先保留首个 C++ 错误，避免被末尾 make/TBE 级联错误挤掉。
11. RuntimeState 的 pending effect 使用原始 action ID，而非限制为 128 字符的业务对象 ID。已用原始失败运行中的 187 字符 ID 验证状态可读，且没有重发该动作。

12. Python/JS 数字往返采用 JSON 数值语义比较：`0.0` 与 `0` 等价，布尔与数字仍不同，大整数失真仍拒绝。原始 action ID、持久化哈希和凭据保持原样。
13. 已回收的原生 runner 非零退出（包括实测 Cube SIGSEGV）记录为可修复失败，保留退出码和诊断。超时或失联仍为 UNKNOWN，不通过重试掩盖不确定效果。

## 已完成的验证

- macOS：干净目录安装固定 Python/npm 依赖、launcher 可执行位、实际 Pi 导入与帮助命令；干净安装已完成，最终源码 inventory 随提交更新。
- Linux aarch64：官方 Node 22.22.3、npm ci，以及 10 项真实 bubblewrap 文件、网络、写回和隔离检查通过。
- 仓库回归：41 项 Python、19 项 Node、TypeScript 检查；原始日志分别保留。
- 选择的上游用例：181 项 Python 协议/模型/恢复测试，56 项 remote job/RSI collect 测试；较早的 175 项 Python；119 项 Node（原 118 通过/1 跳过中的真实 LoopX 用例另行通过）；另一次 RSI controller/extension/model/round 集合 46 项通过。集合可能重叠，不能相加当作独立总数；这不等于完整上游测试集。
- Python 上游测试使用锁定 pydantic 2.13.4 的独立环境。Anaconda readline 扩展导致 pytest 捕获初始化段错误；测试启动时禁用可选 readline 导入后运行，保留原失败记录，没有放宽测试断言。
- 在线 DeepSeek Flash：普通生成、错误评测后修复、Knowledge 命中/空结果、原始 receipt 读取及恢复不重复产生效果通过；真实自定义 Ascend C 生成、精度失败、修复后通过也已有证据。
- 正式 SDK + LoopX CPU Trial 已通过独立 validator，留存 115 条 SDK 事件，无未决效果。它证明托管入口接线，不能替代设备性能测试。
- 真实 NPU 失败矩阵覆盖成功编译/加载、源码与二进制身份、原作业不可覆盖、编译失败、加载失败和超时 UNKNOWN。旧矩阵中修复设备选择之前的 timing 不作为性能结论。

## 当前设备实验与证据边界

- 平台：Ascend910_9382，逻辑设备 1，CANN 9.1、torch_npu 2.10，aarch64 Linux。不是 910B；共享设备上的结果不代表其他平台。
- Vector task（全部字段与仓库示例一致，runner 重新序列化 JSON 排版）：`sdk-durable-final-vector-add`，89 条事件，正常 `finished`，独立 `accepted=true`，五组输入正确，100 个计时样本，无未决效果。最终源码 SHA256 `65c289d081e51e6375696553a4d7e53b1d9553edcf4659e0d177152549b5ba09`。
- Cube task（全部字段与仓库示例一致）：`sdk-durable-final-cube-matmul`，82 条事件，同样正常结束并独立验收通过。最终源码 SHA256 `77d8ced1c895090d2757da1786b35a1cc16a7ba4a8281baaf24e8e8ea8647331`。
- Cube 修复闭环 `sdk-durable-cube-repair` 另保留 256 条事件：编译错误和 runner 崩溃反馈后，模型修复至 `finished`、`accepted=true`。最终示例向模型提供已验证的原生参考，因此这是有参考的生成能力，不是完全无样例的能力声明。
- 原 FP32[128] 40-turn 尝试中，一次 NPU worker 初始化超时触发 UNKNOWN；随后暴露长 action ID 的状态 schema 错误。该失败记录保留，未冒充已跑满 40 turn。
- CANNBench 优化改用固定仓库提交 `d9cb127b40f2c454c8df32cc29c65d0852c1f608` 的 Season 1 Sigmoid 全部 20 个用例。使用原始数据生成器及 CPU golden，对完整输出检查 MERE/MARE 与输入不变，覆盖 FP16/BF16/FP32、尾部、NaN/Inf。新的确认种子仍是相同公开用例，不是隐藏形状。
- 每用例 3 次 warmup、30 次 NPU event 样本，两侧交错运行于独立进程，防止动态库相互覆盖；同一作业内的完整成对测试受设备串行锁保护。
- 这是本地 harness 优化实验，没有向 CANNBench 平台提交，不产生官方分数。NPU event 区间也可能包含主机提交造成的设备空闲间隔，不能当作 profiler 的纯内核时长。

## 尚不应宣称的能力

不宣称所有 Pi TUI/主题/任意第三方扩展已验收；当前覆盖 headless SDK、基础工具、业务工具、恢复、用量及受控插件。在线长会话是否触发真实 compaction 单独核查；合成的上下文/回放测试不能代替这一事实。本轮 NPU 长作业使用现有 RemoteJobs.submit/collect，并保存原始句柄；配置和设备部署仍由可信宿主提供。失联效果仍以原 action ID 对账，不能自动重试。

## 40 回合实验的区分

- `sdk-goal-sigmoid-40turn-v5`：40 个父模型回合，9 个已结算的失败候选，没有通过全 20 用例的原生实现，LoopX 为 `no_progress`。未取得性能提升。
- `sdk-goal-sigmoid-40turn-final`：40 个父模型回合，另有 4 个只读子调用，10 个已结算的失败候选；原生独立 validator 拒绝了保留的 Torch 起点，结果为 `validation_failed`。这些调用不计入后面的成功优化。
- `sdk-goal-sigmoid-optimization-40turn`：提供正确原生起点后，第 4 回合未编辑就提交实验，触发空分解退出；据此修复 `unchanged_source` 提案检查。该运行没有达到 40 回合。
- 后续成功与性能统计仅使用修复后的 `sdk-goal-sigmoid-optimization2-40turn` 及其原始凭据。起点是明确标注的人工单核实现，不是模型从零生成。起点先通过全 20 用例；最初的 `Reciprocal` 版本误差约 0.26%，换成张量 `Div` 后才通过，不放宽原 CANNBench 精度阈值。

原始程序、失败候选、编译日志、逐例 30 个计时样本、model events、RSI acceptance 和独立验证结果均保留；统计脚本从账本重建回合数，不把 SDK `turn_end` 事件数直接当成模型请求数。

## 修复后的 40 回合结果

`KernelCat.createSession` + `LoopXGoal.advance` 的真实运行完成 **40 个父模型回合、0 个子模型回合、514 条 SDK 事件**。LoopX 结果为 `progress` / `validated_progress`，独立 validator 通过，持久化 turn 为 `committed`，没有未决效果。没有观察到在线 compaction，因此不将它列为本轮实测能力。

共有 7 次模型提交的候选评测，以及 1 次周期性保留版本复验。两项源码变化被接受：40 核分工，以及修复后通过全用例的双缓冲流水。周期复验中两侧源码相同，不算新优化，也不把其 A/A 时间波动计作加速。扩大分块的最后一次候选触发设备错误，被拒绝，保留之前正确的 2048 元素分块版本。

最终六段源码已放入 `examples/cann/sigmoid-optimization/solution.py`，SHA256：
`831458d59ad59db391642ae52fbb563065ad3ed33b07218d39cf357534f3f379`。
它以人工合格单核起点为基础，全部优化编辑由在线 DeepSeek 通过 Pi 工具完成。运行中没有从外部修改模型工作区候选。

### 最终源码的独立成对复测

| 对照 | 全 20 用例正确性 | 几何平均加速比（基线时间 / 候选时间） |
| --- | --- | ---: |
| 保守单核原生起点 | 两侧均通过 | 16.9087 |
| Torch，第一组顺序 | 两侧均通过 | 0.5393 |
| Torch，交换两侧顺序 | 两侧均通过 | 0.4864 |
| 两组 Torch 配对合并 | 两组均通过 | **0.5122** |

因此，这次 harness 确实优化了自己的原生起点，但最终整体仍比 `torch.sigmoid` 慢约 **1.95 倍**。没有达到超越 Torch 的结果。每组每例使用 3 次 warmup、30 个显式 NPU event 样本；按用例交错两侧执行，第二组交换 keep/remove 角色，使用新的相同公开用例种子。共享设备及主机提交时间会产生波动；这些是当前环境的本地观测，不是官方分数或纯内核 profiler 数值。

完整逐例均值、原始样本的定位信息、三组成对源码及 measurement ID 保存在 `performance-summary.json` 和 `sigmoid-final-comparison/`。`campaign-summary.json` 从原账本统计所有主要成功和失败运行；`task-input-equivalence.json` 证明 Vector/Cube 运行输入的全部字段与交付示例一致（JSON 排版经 runner 重新序列化）。
