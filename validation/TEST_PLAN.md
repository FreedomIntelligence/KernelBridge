# Pi / SDK / LoopX / NPU 功能验收清单

2026-11-09。导入基线 `87c8fe4`；本轮实现、测试和 evidence 边界见 [验收记录](ACCEPTANCE_RESULTS.md)。通过只覆盖下表明确的场景，不代表任意生成程序都正确。

| 功能 | 放行标准 | 当前验证 |
| --- | --- | --- |
| 安装与版本 | 固定 Pi/npm/Python；实际导入 Pi；Node 所需 API 存在；新目录安装和启动 | Pi 1.5.0；Node Zstd 检查；macOS 干净安装；Linux aarch64 npm 安装和真实工具检查。保留旧运行原绑定 |
| Pi 基础工具 | read/write/edit/bash/grep/find/ls；文件字节准确；模板字面量不经 JS 插值 | 真实 Pi SDK、精确编辑、`${...}`/反引号、搜索与错误参数回归；在线 Vector/Cube 实际写出源码 |
| 文件权限 | 普通任务和 RSI 的 reference 只读；符号链接/越界/旧工具不能绕过 | 原生、旧文件工具、shell 差异写回、scout 角色及 Knowledge raw grep 对照 |
| shell | 独立副本；仅允许普通文件差异写回；网络/外部文件拒绝；取消/超时不部分写回 | macOS Seatbelt；Linux bubblewrap 实机 10 项。空 `/proc` 是明确限制，无宿主回退 |
| 业务工具 | 默认直接调用 evaluate/Knowledge/receipt；Gateway 执行权限、预算、原始凭据 | 在线生成、错误评测后修复、Knowledge 命中/空结果；可选 MCP 超时对齐 |
| 正确性 | 两侧输入存储隔离、冻结答案；保留合法别名；输入修改/dtype/NaN/Inf/整数检查 | CPU 对照及真实 NPU；FP16 Cube、FP32 Vector 五组输入通过；显式容差不能由候选更改 |
| 参数与恢复 | 未声明工具/缺参记未执行失败；数值跨 JS/Python 不误判；原 action ID 不变 | `0.0→0` 真实 Pi 回合回归；bool/数字混用和大整数失真仍拒绝；长 pending ID 可读 |
| 原生编译和运行 | 编译→加载→正确性→计时；源码/二进制身份；各失败阶段可追踪 | CANN 9.1 / Ascend910_9382 实机；现代 CMake、独立 OPP、加载和进程崩溃反馈；超时仍 UNKNOWN |
| 在线 SDK 生成 | task.goal 进入同一 Pi 会话；原生算子五组正确；读 receipt 后正常结束 | 两个最终 task 原文件均 `finished` 且独立 `accepted=true`；不以 step_limit 或模型总结代替 |
| 验收防误报 | 最终源码匹配原始成功 receipt；参考/目标/容差/样本数匹配；无未决效果 | `examples/cann/validate.py`；无凭据、成功后改源码、未决效果的拒绝测试 |
| 远程作业 | 独立运行、保留句柄；丢失 ACK 后收集原作业；无自动重发 | 使用现有 RemoteJobs；56 项 remote job / RSI collect 回归；实际 NPU 生成使用 detached submit/collect |
| SDK + LoopX | LoopX 管理 Goal，Pi 执行；固定 Program/TaskContract；独立 validator | 正式 CPU 单 Trial 已测；真实 NPU 40 父模型回合、514 事件，LoopX progress、独立 validator 通过、无未决效果。单次 advance 不代表自动多 Trial 全覆盖 |
| 性能 | 明确设备/stream；同设备成对测量；全部正确；保留原始样本 | 修复默认设备错误；旧 timing 作废。CANNBench 全 20 用例实验以最终记录为准，无官方成绩声明 |
| 日志与交付 | 版本、任务、events、原 action、候选/二进制哈希、完整编译日志、样本、失败记录 | 本地 evidence 目录及下载的设备日志归档；源码提交不包含 API key、SSH 配置或运行数据 |

## 原问题的固定防回归

- 参考输入污染：错误清零候选必须被拒绝；共享视图与合法原地参考有独立对照。
- 普通任务 reference 可写：通过 Pi 原生、旧工具和 shell 都必须拒绝，原内容不变。
- Knowledge grep 符号链接越界：read 和 grep 一致拒绝库外内容。
- 无效工具破坏恢复：确定未执行的调用必须有失败凭据并完成检查点；空变更实验不得创建无法分解的 pending proposal。
- 固定 MCP 300 秒截断：默认业务工具直连；可选 MCP 使用 Worker 预算；超时后的未决效果保留。
- 原附件里的 `tools.exec_command`、`write_file` 参数和 JS 字符串插值错误：默认只暴露 Pi 工具，真实回合验证工具声明、原始字符串写入、非法调用后的恢复。正式 Vector/Cube 从前几轮开始写候选并评测；不再用仅阅读参考文件的会话充当成功。

正式入口已有串行队列，早先怀疑的“并行 RPC busy”不计为缺陷。

## 可复跑的仓库检查

Python 需要主机依赖和 CPU Torch；Node/npm 已按锁文件安装。命令不调用线上模型或设备。

```sh
npm run check:types --prefix source/apps/agent
node --test validation/*.mjs
python3 -B -m unittest discover -s validation -p 'test_*.py' -v
python3 -B install.py --verify-only
git diff --check
```

当前仓库回归目标为 Python 57 项、Node 25 项，另有类型与交付检查。上游选定测试使用独立锁定环境：181 项模型/原生协议/恢复、56 项远程作业/收集，以及之前的 Python 175 项、Node 119 项和 RSI 46 项集合。各次集合可能重叠，不相加宣称独立总数，也不宣称完整上游套件已经随包交付。

## 实机和在线放行

1. 使用新输出目录，冻结 task、参考文件、可信 evaluator 和模块绑定。
2. 运行 [CANN SDK 示例](../examples/cann/README.md)，检查进程退出码和独立 `acceptance.json`；保留第一次失败及后续修复，不合并成一次成功运行。
3. 长性能目标绑定 LoopX 和匹配的 RSI adapter，记录每个模型回合、候选哈希、测量与接受结果。40 模型回合不等于 40 次设备实验。
4. 正确性通过后才比较性能；初始 baseline、候选、设备、计时范围、完整用例和原始样本都必须明确。远程结果不明时收集原句柄，不重发。
5. 交付时验证新目录安装，更新源码 inventory，扫描凭证，再提交推送。没有被本轮覆盖的 Pi TUI、任意第三方插件、其他 SoC、隐藏用例和自动多 Trial 生命周期保留为未覆盖项。

本次默认 Goal 入口的 Vector / Cube 各 20 轮测试及发现、修复见 [20 轮回归](NEW_FLOW_20TURNS.md)。
