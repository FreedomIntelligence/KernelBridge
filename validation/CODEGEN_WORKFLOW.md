# 算子开发工作流

KernelBridge 默认使用 Pi SDK 执行模型与基础工具回合，LoopX 负责 Goal 的目标、任务调度与完成判定。设备评测、知识库、IR 和 profiler 通过业务工具接入。

## 组件职责

| 组件 | 职责 |
| --- | --- |
| Pi SDK | 模型上下文、会话、read/write/edit/bash/grep/find/ls、工具调用与原生 session |
| KernelBridge | 文件权限、进程隔离、积分预算、执行身份、源码冻结、评测凭据与恢复 |
| LoopX | Goal、Todo、配额、跨 Trial 调度、独立验证与完成状态 |
| 业务工具 | 编译、正确性、性能测量、知识检索及诊断采集 |

## 执行流程

1. 提交 task 和 profile，冻结目标、测试输入、参考、初始实现、权限与预算。
2. 创建 LoopX Goal，由 Pi 读取 AGENTS.md、任务附录和已有源码。
3. 调用 baseline_check，确认初始实现与目标环境。
4. Pi 生成或修改候选，使用 operator_check 获取编译和正确性反馈，按诊断修复。
5. 功能任务调用 operator_evaluate 完成验收；性能任务使用 RSI 成对测量与独立确认。
6. KernelBridge 按源码哈希冻结通过验收的候选；LoopX 执行独立验证，完成 Goal 或按配置继续。
7. 导出候选、原生 session、日志、评测凭据与 manifest。

## 功能生成与性能优化

Vector/Cube 默认使用 generate，按编译和正确性验收。optimize 从初始实现开始，按任务声明的性能协议比较候选。session-only 提供单次 Pi SDK 会话。Goal 与任务模式分别配置。

profile 的 goal.max_trials 控制最多推进多少个 Trial，policy.max_steps 控制每个 Trial 的模型回合数，credits 控制积分预算。complete_on_acceptance 为 true 时，独立验收通过即完成 Goal；为 false 时，在 Goal 配额和剩余预算允许的范围内继续。

## 状态与源码

completed 表示 Goal acceptance 已通过；progress 表示当前 Trial 已产生通过独立验证的候选；invocation_limit 表示本次调用达到 Trial 上限。validation_failed 保留验证结果，recovery_required 保留原执行身份供对账。

交付源码位于 candidate/solution.py，candidate/identity.json 记录其 SHA256 与 Program 引用。workspace 保存实际工作区，评测凭据关联输入、参考、编译产物与候选身份。

完整提交方式见 [CANN 示例](../examples/cann/README.md)，SDK 接口见 [KernelCat](../kernelcat/README.md)。
