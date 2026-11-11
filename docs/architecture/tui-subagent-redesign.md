# TUI 与子代理重构：排查结论、设计与 TODO

日期：2026-11-10。基线：`79f2ed2` 加工作区未提交改动（会话选择器、`list_files`/`search_text`、`policy: auto`、委派提示词）。Pi `@earendil-works/pi-coding-agent@1.5.0`，社区插件 `pi-subagents@0.87.0`。

范围：普通 codegen 的 TUI 交互，以及子代理（`delegate`）的执行模型和展示。不含 NPU 测试后的 profiler/IR 调用链（另行核查），也不改 Goal、Trial、评测协议。

> 本文第 1–5 节保留最初排查和设计。当前行为以实施状态、`docs/tui.md` 与最新验收记录为准。并行修改按文件分工；源码封存后统一构建和验收，`PIN_MANIFEST.json` 使用原子写入。另一个仍活跃的验收 checkout 不参与清理。

---

## 0. 结论摘要

1. **TUI 的 "↓ Jump to latest message"**：这不是渲染 bug。Pi 默认使用全屏 alt-screen（`tuiMode: "fullscreen"`）。用户滚轮或翻页离开底部后，视图停止跟随，新输出只追加在视口下方。Claude Code 默认直接在主屏渲染，使用终端原生滚动，所以不会出现这种情况。是否切换模式需要你决定（见 §8）。
2. **子代理慢、看不到进展**：根因在协议层。整条链路按"单通道、单 permit、严格串行"设计，有五处串行点（见 S1）。同一回复里的多个 `delegate` 只能一个接一个执行。用 fixture 复现时，两个子代理耗时约等于单个的 2 倍。
3. **子代理展示与 Claude Code / Codex 差距大**：调用块会铺开整段任务原文；工具块内没有实时进度和耗时；子代理状态分散在三处显示；Ctrl+O 展开后是原始 JSON；空闲后页脚仍残留子代理行；没有"等待 N 个子代理"和完成通知。
4. **功能缺口**：子代理只读，不能跑测试或 shell。用户实际问的"启动子代理做一些测试"，现在的子代理做不了。不支持后台运行，也没有 wait/stop。默认限额偏小（6 次模型、16 次读取、120s），之前已经触顶过。
5. **一个真实 bug（根因已确认）**：回合结束后界面报 `Error: Pi input has no durable host receipt`。和子代理无关，普通对话也会出现。原因是启动阶段的竞态让同一条输入被 Pi 提交了两次（见 T5）。
6. **要不要把 Python 改成 TS**：**不建议全量迁移。** 上面的问题都不是语言造成的。理由和推荐分工见 §4。
7. **流式与工具输出的形式**：流式链路本身是通的，bash 输出也是实时的，但有三处 SSE 缺陷：
   - 防凭据回显的扣留让文字始终晚 35 个字符出现（中文约一整行）；
   - 思考内容（`reasoning_content`）被丢弃；
   - delta 不按调用 id 过滤，子代理一旦并行就会串台。

   工具输出用的是 Pi 原生样式，和 Claude Code / Codex 差距明显：没有 ⏺/⎿ 层级和成功/失败状态色；read 折叠后什么都不显示；write 直接铺出文件内容；错误样式不一致且暴露绝对路径；working 行没有耗时、tokens 和 Esc 提示。见 §2.3、§3.5、§5.7。

## 实施状态（2026-11-11，验收树 `codex/harness-20turn`）

并行、后台通知、取消后继续输入、tester/worker 与展示功能已落地。§6 区分已验证项、采用的替代设计和仍待补充的验收，不能把功能实现等同于原计划的全部测试完成。当前证据来自 `/Users/jerry/.codex/worktrees/harness-20turn/KernelBridge`；旧树的 48 列验收单独标明。完整边界见 [验收记录](../../validation/tui/ACCEPTANCE.md)。

| 项 | 结果 |
| --- | --- |
| P0-1/2/3 | 重复输入与流式前缀扣留已修复，有输入结算、流式单元及 PTY 证据；本次使用独立 worktree |
| 阶段 1 / 1B | 展示已实现。单子代理步数、读取和时长默认不设限，只保留 `max_concurrent` 默认 6（超出排队）；接近上下文上限时保留最后一次总结调用。`/agents` 显示可用类型与运行记录。Working 显示耗时和 Esc，不估算流式 tokens。逐工具成功/失败完整 PTY 矩阵及真实 thinking 模式仍待补 |
| 阶段 2 | 社区 pi-subagents 保留；全为 delegate 的批次并行，混合批次串行。当前 PTY 验证 Journal 调用区间重叠、乱序结果、Esc 后继续输入；预算边界与未知效应有定向测试，独立高并发 `propose` 压测仍待补 |
| 阶段 3 | 执行层 `background` 表管理生命周期，不新增 `ChildScheduler` 类；用持久化 `follow_up` 通知、`agents_status` 与 `stop_agent` 代替阻塞式 `wait_agents`。父输入及下一条输入在子代理仍运行时均可结算，已由 PTY 验证；真实模型也验证了父先返回、两个唯一通知及最终汇总 |
| 阶段 4 | 自定义定义在工具规格封存前加载；`kernel-tester` 仅前台并阻止父写入，后台命令用隔离的 `kernel-worker`。worker 要求 committed HEAD，`git stash create` 快照不含未跟踪文件；完成后清理 checkout、保留分支与 diff，未知效应保留 checkout。实执行、停止和恢复由定向测试覆盖 |
| 阶段 5 | 默认 `--tui-mode regular`，`fullscreen` 可选；当前并行和真实模型 PTY 使用默认模式，不等于重新跑完所有历史模式/resize 矩阵 |
| V-3 | 真实 DeepSeek 五项行为跨两个新会话通过，见 `.validation/subagent-final-validation.json`。第一会话中后续显式委派的歧义校验词失败保留，不计为通过；不是单会话全程通过 |

后续真实验收还修复了并发预算 CAS 冲突、generation 误认导致的流串台、
首帧原生预算页脚、中断收尾窗口拒收输入，以及展示队列溢出时关闭控制连接的问题。
effect、control、observer 分连接；有界预览允许合并，最终完整答复来自原 effect。
长快照/恢复有独立 ACK 等待上限，超时保留原身份而不重派。

验收采用原 Journal 的重叠时间和“后台仍运行时父输入已经完成”的断言证明并行/后台，
不以繁忙开发机上的固定 1.6 倍墙钟阈值判定。流式 token 数没有可靠中途计数时不展示伪估值。
reasoning 的解析和展示链由协议测试覆盖；真实模型验收使用配置中的默认 DeepSeek 路由，
不宣称已覆盖所有 provider 的 thinking 模式或与 Codex/Claude Code 每个细节完全相同。
本轮没有由上述 TUI 验收得出的 NPU 性能结论；scatter 的 eager-NPU 比较与优化回合另行记录。

---

## 1. 排查方法与证据

- **本地 fixture 复现**：写了一个脚本化的 OpenAI 兼容 provider，不调用真实模型。父模型一次发出两个 `delegate`，每个子代理依次执行 `list_files` → `search_text` → `read_file` → 回答，每次模型调用延迟 1.5s。在 PTY（100×30）里驱动 `./kernelagent`，记录了 246 帧屏幕。
- **代码定位**：Pi / pi-tui / pi-agent-core / pi-subagents 的 dist 源码，以及本仓库的 Node harness、application host 和 executor authority。
- **输出形式复现**：用第二个脚本化 SSE provider 依次调用 read/bash/grep/edit/write/ls，在 100×90 终端中记录逐帧画面和每行颜色（§2.3）。T5 的根因是在 `/tmp` 的仓库副本里给 `input` 处理加调用栈日志后确认的；工作区本身没有改动。
- **参照来源**：Codex 多代理工具定义（`.validation/prompt-sources/codex-tools.rs`：`spawn_agent`、`send_message`、`followup_task`、`wait_agent`、`list_agents`、`close_agent`、`interrupt_agent`、`resume_agent`），Claude Code 的 Agent 工具描述与子代理提示词整理（`.validation/prompt-sources/*.md`）。我们只对齐行为，不复制实现。

复现中的关键帧（节选）：

```
 我会分两个子代理并行调查。
 子代理 kernel-scout · read_only
 CHILD_A 梳理开发流程：请阅读相关文件并总结。请阅读相关文件并总结。请阅读…   ← 整段任务原文铺开
 。
── ⠴ Working ─────────────────────────────────────────────                    ← 英文
  1 active agent · ↓ 100 window · 220 spent · ↓/← to inspect                  ← pi-subagents FleetView
demo · 执行中
子代理 kernel-scout · 执行中 · 2 次模型 / 2 次读取 · CHILD_A 梳理开发流程…   ← 产品页脚，第三处
```

```
 kernel-reviewer · 已完成 · 4 次模型 / 3 次读取
 CHILD_B 结论：…
 （Ctrl+O 展开详情）
 汇总：两个子代理均完成。开发流程与验证流程一致。
Error: Pi input has no durable host receipt                                  ← T5
demo · 等待输入
子代理 kernel-scout · 已完成 · …                                             ← 空闲后残留
子代理 kernel-reviewer · 已完成 · …
```

Ctrl+O 展开后看到的是原始回执 JSON：`{"outcome":{"data":{"advisory":true,"agent":"kernel-reviewer","completion_reason":"answer",...`。

时间线：CHILD_B 的第一次模型调用要等 CHILD_A 完全结束后才开始。Journal 中两个子代理的 4 次模型调用前后不重叠。

---

## 2. 问题清单

严重度：P0 = 错误或数据风险，P1 = 核心体验或功能缺口，P2 = 打磨项。

### 2.1 TUI

| # | 严重度 | 现象 | 根因 / 位置 |
|---|---|---|---|
| T1 | P1 | 运行中出现 "↓ Jump to latest message · Ctrl+End"，新输出不跟随 | Pi 默认 `tuiMode: "fullscreen"`（`pi-coding-agent/dist/core/settings-defaults.js:61`）。全屏模式会开启鼠标上报（`pi-tui/dist/tui-alt-screen.js:16`），滚轮事件交给应用处理：`ScrollView.scrollBy/scrollTo` 离开底部后 `followingEnd=false`（`pi-tui/dist/components/scroll-view.js`），之后只有 Ctrl+End 或提交新输入才会回到底部。终端原生的选中和复制也因此要按住 Shift。没有滚动时 fixture 中视图跟随正常，所以触发条件是用户滚动。我们的 `runtimeServices` 没有设置 `tuiMode`，用户也改不了 |
| T2 | P1 | 中英混杂：`Working`、`escape interrupt · ctrl+c/ctrl+d clear/exit …`、`Tool output: expanded`、`Jump to latest message` | Pi 原生文案。目前 patch 只改了 header 两行（`patches/pi-coding-agent-1.5.0-host-actions.patch`）；滚动提示在 `dist/modes/interactive/tui-renderer.js:16`，不在已 patch 的文件里 |
| T3 | P1 | 子代理状态有三处：调用块、FleetView 小部件（`1 active agent · ↓/← to inspect`）、产品页脚两行；空闲后页脚仍保留最后两条 | FleetView 来自 `pi-subagents/src/tui/fleet-status.js:773`；产品页脚 `product-ui.mjs` 无条件渲染 `state.subagents.slice(-2)`。FleetView 显示的 spent 会在第二个子代理启动时归零，而且 `↓/←` 在我们的配置下是否可用未验证 |
| T4 | P1 | Ctrl+O 展开 `delegate` 结果时显示原始回执 JSON | `renderReceiptResult` 的 expanded 分支直接输出 content 文本（即 JSON.stringify 后的 outcome），`product-ui.mjs:88` |
| T5 | **P0** | 回合结束后报 `Error: Pi input has no durable host receipt`，普通对话同样出现 | **根因已确认**（在副本中给 `input` 处理加日志复现）。patch 后的 `InteractiveMode.run` 会先 `await hostActions.initial()`，然后才进入主循环。界面已显示"等待输入"时，用户如果在 `initial()` 完成前提交：① 编辑器的 `submit` 把输入记为 queued 并加入 `delivered`；② `initial()` 发现这条 queued 输入，把它当作 `initialMessage` 执行（`interactive-mode.host.js:922`）；③ 编辑器提交还留在 `getUserInput()` 中，回合结束后主循环再执行一次同一段文字（`:944`）。第二次因回执已被消费而报错，等于宿主挡下了一次重复执行。修复：`initial()` 跳过 `delivered` 中的回执，或在 `initial()` 完成前不接受编辑器提交 |
| T6 | P3 | 抓帧时偶尔看到重复残行 | Pi 使用同步输出（`\x1b[?2026h…l`，`pi-tui/dist/tui-alt-screen.js:21`），pyte 不支持这个序列，抓到的是半帧。真实终端不受影响，**不是 bug**。PTY 测试应等画面稳定后再断言 |
| T7 | P2 | 启动时先显示 Pi 原生页脚（`0.0%/66k (auto) demo`），宿主连接后才切换为产品页脚 | `installProductUI` 在 `session_start` 之后才安装 |
| T8 | P2 | `/subagents` 是静态文本：`已启用：true`（裸布尔值）、整段任务原文，且仍写着"依次执行" | `host-actions.mjs:413-424` |

### 2.2 子代理

| # | 严重度 | 现象 | 根因 / 位置 |
|---|---|---|---|
| S1 | P1 | 同一回复中的多个 `delegate` 串行执行，总耗时按个数线性增长 | 五处串行点：① `runtime-factory.mjs:81` 强制 `toolExecution = 'sequential'`（Pi 本身支持并行批次和按工具声明 `executionMode`，见 `pi-agent-core/dist/agent-loop.js:373`）；② `runtime-factory.mjs:103` 所有 RPC 走同一条 `serialRpc` 通道；③ `loopx/model-control.ts:80` 只允许一个子 permit；④ `authority.py:524-638` 用单个 `remote_thread`/`child`；⑤ `delegation.py:49` "one foreground delegation at a time"。宿主侧每个连接的 `Connection.run` 逐条处理请求，所有效应排进一个 `application-executor` 线程（`application_host.py:113,886`）。客户端 `client.mjs` 已经按 id 匹配响应，支持乱序返回 |
| S2 | P1 | 不支持后台：父代理必须等子代理结束；没有 wait / stop / status 工具，没有完成通知 | 协议只支持前台模式：`delegate` 调用必须在当前 turn 内结算，`end_turn` 要求 `remote_thread is None` |
| S3 | P1 | 调用块里没有实时进度（当前工具、读取次数、tokens、耗时），也没有完成行 | `directTools` 的 `execute` 没有接 `onUpdate`（`runtime-factory.mjs:39`）；宿主投影里有 `steps`/`tool_calls`/`latest_tool`，但没有耗时和 tokens |
| S4 | P1 | 调用块显示整段任务原文 | `delegate` 参数没有短标题。Claude Code 的 Agent 工具有 `description`（3–5 词）和 `prompt` 两个字段；我们的 `renderDelegateCall` 只能展示 `task` |
| S5 | P1 | 子代理只读，不能运行测试或 shell，不能写文件 | `READ_TOOLS` 白名单（`delegated.py:24`），能力上限只允许 `kernelcat_child_effect`。和用户的实际诉求（"启动子代理做一些测试"）不符 |
| S6 | P1 | 默认限额小：`max_steps=6`、`tool_limit=16`、`timeout=120s`；插件默认 `defaultTimeoutMs:60000` 与宿主配置不一致 | `configuration.py:96-103`、`plugin.mjs:29`。之前真实 DeepSeek 测试在第 6 步撞上限 |
| S7 | P2 | 子代理类型固定为 `kernel-scout`/`kernel-reviewer`，不能通过项目或用户文件自定义 | `plugin.mjs:23` 写死；Claude Code 支持 `.claude/agents/*.md`，Codex 有 agent_type |
| S8 | P2 | 父模型收到完整回执 JSON（含 receipt id、plugin 名等），占上下文 | 工具结果 content 就是 outcome JSON。Claude Code 返回子代理的最终文本和简短用量 |
| S9 | P2 | 提示词明确写着 "Children currently execute one at a time…do not…promise concurrent execution" | `delegation-prompt.mjs:8`，需要随实现分阶段更新 |
| S10 | P2 | 子代理 tokens 和耗时不进入状态投影；`/subagents` 无法逐个查看报告 | `application_host.py:427` 投影字段不全 |

### 2.3 输出形式：SSE 流式与工具输出

复现方式：脚本化 SSE provider 每 80ms 发 3 个字符，依次调用 `ls`、`read`、慢速 `bash`（6 行，每行间隔 0.7s）、`grep`、长输出 `bash`、`edit`（失败）、`write`（24 行）、`read`（文件不存在），最后输出 Markdown 总结。终端 100×90，在 `/tmp` 的仓库副本上运行。

当前实际渲染（节选，颜色另行记录）：

```
 我先看一下项目结构，然后读取实现文件。
 ls .
 solution.py
 read solution.py                                   ← 折叠时没有任何结果行
 grep /def / in .
 solution.py:1: def add(a, b):
 $ python3 -c "…"; for i in $(seq 1 30); do echo line $i;
 done
 ... (26 earlier lines, ctrl+o to expand)           ← 英文；保留尾部 5 行
 line 26 … line 30
 Took 0.2s
 edit solution.py
 Error: Found 2 occurrences …                       ← 红色
 write notes.md
 # Notes                                            ← 直接铺出文件前 10 行
 …
 ... (14 more lines, 24 total, ctrl+o to expand)
 read missing.py
 Error: ENOENT: no such file or directory, access '/private/tmp/…/missing.py'   ← 默认色，绝对路径
── ⠇ Working ──────────────────                     ← 无耗时 / tokens / Esc 提示
```

| # | 严重度 | 现象 | 根因 / 位置 |
|---|---|---|---|
| O1 | P1 | 流式文字始终比实际收到的晚若干字符；每段最后一截（DeepSeek 下为 35 个字符，中文约一行）要等这段结束才一次性跳出 | `capabilities/streaming.py` 的 `emit_text` 为防止凭据被分帧回显，固定扣留 `len(secret)` 个字符。安全性不需要扣这么多：只需扣留"恰好是密钥前缀"的最长后缀（KMP 式），同样安全，延迟几乎为零 |
| O2 | P1 | 开启思考模式时，思考内容完全看不到，只剩一条长时间无反馈的 Working | `streaming.py` 只解析 `delta.content`，丢弃 `reasoning_content`；Pi 侧也没有 `thinking_delta` 通道。当前默认是非思考模式，所以暂时不明显 |
| O3 | P1（并行前置） | 所有 `stream()` 调用订阅的都是全局 `model_delta`，不按 `generation_id` 过滤 | `application/worker.mjs:35`、`runtime-factory.mjs:134`。串行时无害；子代理并行、Goal 评审或压缩并发时会串台 |
| O4 | P1 | 没有层级和状态：助手文字、工具调用和结果都是平铺的，工具头没有运行中/成功/失败的状态标记 | 使用 Pi 原生 renderer。Pi 支持按工具覆盖 `renderCall`/`renderResult`/`renderShell`，渲染上下文提供 `executionStarted`、`argsComplete`、`state`、`invalidate`（`core/extensions/types.d.ts:366,488-537`） |
| O5 | P1 | `read` 折叠后什么都不显示；`write` 直接铺出文件内容；`grep` 直接列出匹配行 | `pi-coding-agent/dist/core/tools/renderers/read.js:83` 等。Claude Code 折叠时只给一行摘要（读了几行、写了几行、找到几处），展开才看全文 |
| O6 | P1 | 错误样式不一致：edit 的错误是红色，read 的 ENOENT 是默认色，还带绝对路径 | 不同 renderer 各自处理错误 |
| O7 | P2 | 连续的读、搜、列目录各占一块，探索阶段刷屏 | Pi 每个工具调用一个组件。Claude Code 新版和 Codex 会把连续的探索合并为一行（Codex 显示为 `Explored`） |
| O8 | P2 | Working 行是英文，没有耗时、tokens 和 Esc 提示 | Pi 原生 loader；扩展 API 有 `setWorkingMessage`/`setWorkingIndicator`/`setHiddenThinkingLabel`（`types.d.ts:97-110`） |
| O9 | P2 | 文案英文：`earlier lines`、`more lines … total`、`Took`、`Elapsed`、`Tool output: expanded` | Pi renderer 文案 |
| O10 | P2 | Markdown 代码块显示 ```` ``` ```` 围栏；标题的 `#` 被去掉但层级不明显 | Pi Markdown 主题，可通过 markdown theme 调整 |

做得好的地方，保留：bash 输出是实时流式的（tick 1→6 逐行出现）；工具调用参数完整后才显示工具头，没有半截参数；Markdown 与代码高亮可用；Pi 使用同步输出，不会撕裂。

---

## 3. 目标交互（对齐 Claude Code / Codex 的行为，不复制实现）

### 3.1 前台并行（阶段 2）

```
 我分两个子代理并行调查：开发流程和验证流程。

 ● kernel-scout · 梳理开发流程
   ⎿ search_text "def build" · 读取 3 · 模型 2 · 1.2k tokens · 18s
 ● kernel-reviewer · 核对验证流程
   ⎿ read_file validation/tui/run.py · 读取 5 · 模型 3 · 2.4k tokens · 21s

 ✻ 等待 2 个子代理完成 · 21s · Esc 中断全部
```

完成后调用块收拢成一行，Ctrl+O 展开显示 Markdown 报告：

```
 ● kernel-scout · 梳理开发流程
   ⎿ 完成 · 读取 6 · 模型 4 · 3.1k tokens · 42s
 ● kernel-reviewer · 核对验证流程
   ⎿ 部分结果（读取上限）· 读取 16 · 模型 6 · 5.0k tokens · 1m05s
```

### 3.2 后台（阶段 3）

```
 ● kernel-scout · 全仓扫描旧接口（后台 · a3f2）
   ⎿ 已启动，完成后通知
 …用户继续对话…
 ⏺ 子代理 "全仓扫描旧接口" 完成 · 3m12s · 读取 41          ← 通知条目，自动触发父代理续写
```

空闲页脚只保留一行：`后台子代理 1 运行中 · /agents 查看`。没有子代理时不显示这一行。

### 3.3 `/agents` 面板

使用和会话选择器同风格的原生组件：表格列出 id、类型、标题、状态、耗时、读取次数和 tokens。Enter 查看报告，`s` 停止，Esc 返回。

### 3.4 接口对照

| 能力 | Claude Code | Codex | KernelBridge 目标 |
|---|---|---|---|
| 启动 | `Agent{description, prompt, subagent_type, run_in_background?}` | `spawn_agent{message, agent_type, model?, …}` | `delegate{agent, description, task, mode?, background?, max_steps?}` |
| 并行 | 同一条消息里的多个 Agent 调用并发执行 | 多个 spawn | 同一回复里的多个 `delegate` 并发执行（阶段 2） |
| 等待 / 状态 | 自动通知 | `wait_agent` / `list_agents` | 自动通知 + `wait_agents` / `agents_status`（阶段 3） |
| 停止 | 停止后台任务 | `close_agent` / `interrupt_agent` | `stop_agent` + Esc（阶段 3） |
| 写入 / 测试 | general-purpose 拥有全部工具，可选 worktree 隔离 | worker 角色 | `kernel-worker`（阶段 4，需决策） |
| 自定义类型 | `.claude/agents/*.md` | 配置中的 agent 类型 | `.kernelbridge/agents/*.md`（阶段 4） |

### 3.5 流式与工具输出（对齐 Claude Code 的呈现约定）

参照的约定（以当前公开版本的界面为准，只对齐行为和信息密度，不复刻实现）：

- **Claude Code**：
  - 每条助手消息和每个工具调用前有 `⏺`，运行中闪烁或灰色，成功绿色，失败红色。
  - 工具头写成 `工具名(主要参数)`，结果缩进挂在 `⎿` 下。
  - 折叠时只给一行摘要：读了 N 行、写入 N 行、更新了 +a −b 并附带 diff、找到 N 处；bash 给几行输出再加 `… +N lines (ctrl+o to expand)`。
  - 等待行形如 `✻ 动词… (耗时 · tokens · esc to interrupt)`；Ctrl+O 看完整输出。
- **Codex CLI**：
  - 连续的读、搜、列目录合并为一条 `Explored`，下面列出 Read / Search / List。
  - 命令显示为 `Ran <cmd>`，下挂截断输出；编辑显示为 `Edited path (+a -b)` 加 diff。
  - 等待行 `Working (Ns • esc to interrupt)`。

目标效果：

```
⏺ 我先看一下项目结构，然后读取实现文件。

⏺ 浏览 1 个目录 · 读取 1 个文件 · 搜索 1 次  (Ctrl+O 展开)

⏺ Bash(for i in 1 2 3 4 5 6; do echo tick $i; sleep 0.7; done)
  ⎿  tick 4                                   ← 运行中实时滚动末尾几行
     tick 5
     tick 6
     … 上方还有 3 行 · 4.4s · 退出码 0

⏺ Update(solution.py)
  ⎿  已更新 solution.py：+1 −1
       2 -    return a - b
       2 +    return a + b

⏺ Update(solution.py)                          ← 红色 ⏺
  ⎿  失败：匹配到 2 处，需要更长的上下文才能唯一定位

⏺ Write(notes.md)
  ⎿  已写入 24 行（Ctrl+O 预览）

⏺ Read(missing.py)                             ← 红色 ⏺
  ⎿  失败：文件不存在 missing.py                ← 项目相对路径

⏺ ## 结果 …（Markdown 正文，流式输出）

✻ 执行中… (12s · ↓ 1.2k tokens · Esc 中断)
```

要点：

- 每个工具调用固定"头 + ⎿ 摘要"两层，状态由 ⏺ 的颜色表达。
- 折叠摘要只有一行；全文、完整 diff 和文件内容放在 Ctrl+O 里。
- bash 运行中显示实时尾部；结束后，测试类命令保留尾部（失败信息通常在尾部），其他命令保留头部。
- 路径一律显示项目相对路径。
- 所有文案中文化，统一使用 `Ctrl+O 展开`。

---

## 4. 语言与架构决策：要不要把 Python 改成 TS

**结论：不做全量迁移。** Python 保留为权威层（authority），交互、编排和展示放在 TS 侧。

理由：

1. **问题不是语言造成的。** S1 的五处串行点分布在两种语言中，本质是协议设计。T1–T8 和 S3/S4 的展示问题全部在 TS 侧。用 TS 重写 Python 解决不了其中任何一个。
2. **迁移成本与风险不对等。** 非生成 Python 约 6.4 万行（执行器 4.0 万，其中评测协议 2.8 万；知识库 1.4 万），TS/mjs 约 1.45 万行。Python 侧承载 Journal、预算预留、回执、未知效应隔离、凭据隔离、NPU 评测和知识库，并由 28 个 Python 验收文件和多轮 PTY / 真实模型验收约束。重写等于从头重新验证这些不变量。
3. **凭据边界是有意设计的。** 模型和设备凭据只留在宿主（Python）进程，worker 拿到的是最小环境。子代理的模型调用也必须经过宿主，并发问题应在宿主里用 I/O 线程解决（HTTP 是 I/O 密集，GIL 不是瓶颈）。
4. **SSE 已经端到端打通。** 父模型流式链路是 Python httpx SSE（`capabilities/streaming.py`）→ 宿主广播 `model_delta`（`application_host.py:249`）→ Pi `text_delta`（`runtime-factory.mjs:134`），bash 输出也是实时的。现存的三个 SSE 缺陷（O1 固定扣留 35 个字符、O2 丢弃思考内容、O3 delta 不按调用过滤）都是各自几十行的局部修复。缺的**子代理进度事件流**属于事件协议问题，在现有通道上增加 `subagent_progress` 事件即可。工具输出的形式全部由 TS 侧 renderer 决定（§5.7）。这些都不需要换语言。

推荐分工：

| 层 | 语言 | 职责 |
|---|---|---|
| 展示 / 交互 | TS | 工具块渲染、等待指示、通知、`/agents` 面板、文案、滚动模式 |
| 子代理编排 | TS | 同一回复的并行批次、子会话运行器、进度聚合、取消传播 |
| 准入 / 记账 | Python | permit、预算、Journal、回执、未知效应隔离、模型 HTTP、凭据 |

可选的局部 TS 化：pi-subagents 只被用作"子会话运行器"，大部分功能（async、intercom、workflow、FleetView）被我们关掉或绕开，而且它的 UI 会漏到界面上（T3）。可以评估用 Pi SDK `createAgentSession` 自建约 300 行的子会话运行器来替代它，换取对并发、进度事件和取消的完全控制。先做 spike 再决定（TODO 2-0）。

---

## 5. 设计

### 5.1 并行子代理（前台）

**Node / Pi 侧**

- **工具并行**：把 `agent.toolExecution` 设为 `'parallel'`，同时给除 `delegate` 以外的所有工具声明 `executionMode: 'sequential'`。按 Pi 的规则（任一 sequential 工具会让整批串行），只有"全部是 delegate"的批次才会并行；混合批次自动退回串行，现有语义不变。
- **RPC 通道**：`serialRpc` 改为 `laneRpc`。默认 lane 是父 lane，保持原有顺序；每个 child permit 一条 lane，lane 内有序，lane 之间并发。父 lane 的 `end_turn` 必须等所有子 lane 清空。
- **`controlledModels`**：单个 `child` 改为 `Map<permit, {routes}>`；`plugin_begin` 不再因为另一个 permit 存活而拒绝，只校验每回复数量和总数上限。
- **插件桥** `plugin.mjs`：`execute` 本身已经按 requestId 隔离；需要确认 pi-subagents 能否同时处理多个 REQUEST（TODO 2-0）。

**Python 侧**

- **`SubagentEffect`**：单个 future 改为 `dict[action_id, Future]`；`arm`/`resolve`/`clear` 都按 id 操作。
- **`ExecutionRPC`**：`remote_thread/remote_action/child` 改为 `children: dict[permit, ChildRun]`。`plugin_begin` 只能为当前回复里尚未开始的 `delegate` 调用发 permit，按调用顺序发放。结算时结果写入对应槽位；`outcomes` 只在前缀连续时追加，保证 Journal 和 Pi 看到的顺序与模型回复一致。
- **宿主请求并发**：`Connection.run` 对 `child_model`/`child_tool`/`plugin_settle` 按 permit 派发到对应的 lane 线程，响应带 id 乱序返回。父请求仍走 `application-executor` 线程。Journal 写入复用现有 `RLock`。需要核实 store 的预算预留在并发 `propose` 下是原子的（`_parallel_dispatch` 已有并发工具批次，可作参考，TODO 2-2）。
- **不变量**（必须有测试）：
  - 每个子代理的未知效应独立隔离；任一子代理有未知效应，父代理都不能 `end_turn`。
  - Esc 取消所有存活子代理，已知取消的子代理正常结算，未知的保持隔离。
  - `max_per_turn` 按已开始的数量计数，不按已结算的数量。
  - 并发可能让 Goal token 上限超出最多"在途调用数"次调用。文档写明，或在 `child_model` 准入时按在途预留额度计入。

### 5.2 后台子代理

- **工具**：`delegate{background: true}` 立即返回 `{agent_id, status: "running"}`；新增 `wait_agents{ids?, timeout_s}`、`agents_status{ids?}`、`stop_agent{id}`。
- **归属**：后台子代理不能绑定在父 turn 上。在宿主新增 `ChildScheduler`，管理 lane、限额（新增 `max_concurrent`，默认 4）和生命周期。子任务 id 为 `…/agent/<id>`，所有效应记在子任务下。`delegate` 动作本身以 `{status: "started"}` 结算。
- **通知**：子代理结束时，宿主写入一条输入 `kind: "notification", initiator: "subagent"`，复用现有输入队列 `next()`/`deliver()`。父代理空闲时触发新 turn（与 Goal continuation 相同）；父代理忙碌时在下一个边界作为 follow-up 送达。通知正文是子代理的最终报告和简短用量。T5 的修复必须先完成，因为通知同样是非 TUI 来源的输入。
- **退出与恢复**：
  - `on_detach: pause`：存活子代理被中断，在途调用记为未知并隔离，状态 `interrupted`，下次 attach 时显示。
  - `on_detach: continue`：headless worker 继续运行。
  - 宿主重启：绝不重跑子代理，未完成的标记为 `interrupted` 并通知父代理。

### 5.3 子代理类型与权限

| 类型 | 工具 | 并行 | 说明 |
|---|---|---|---|
| `kernel-scout` | list/search/read、技能、回执 | 是 | 现有类型 |
| `kernel-reviewer` | 同上 | 是 | 现有类型 |
| `kernel-tester`（新增） | 只读工具 + `bash` | 否（与写入方互斥） | 跑测试和诊断。project 模式下 bash 按用户权限在本地执行，无法强制只读，需要在文档中明示 |
| `kernel-worker`（新增，需决策） | 读写 + bash | 视隔离方式而定 | 两种方案：A. 共享工作区，前台、与父代理互斥；B. 在 git worktree 中隔离，返回 diff 或分支，由父代理合并（对齐 Claude Code 的 worktree 隔离）。推荐 B |

自定义类型从 `.kernelbridge/agents/*.md`（项目）和 `~/.kernelbridge/agents/*.md`（用户）读取，frontmatter 包含 `name`、`description`、`tools`、`max_steps`、`timeout`。`delegate` 的工具描述动态列出可用类型。能力上限仍由宿主强制执行。

**默认限额调整建议**：`max_steps` 6→20，`tool_limit` 16→64，`timeout` 120→600s；后台默认 1800s。插件侧的 `defaultTimeoutMs` 统一使用宿主值。

### 5.4 展示层

- **`delegate` 参数**新增必填的 `description`（≤ 40 字符）。调用块显示 `● <agent> · <description>`，`task` 原文只在展开时显示。
- **实时进度**：`directTools` 的 `execute` 接收 `onUpdate`。宿主新增 `subagent_progress` 事件（permit、当前工具和参数摘要、读取次数、模型次数、tokens、开始时间），收到后调用 `onUpdate`；耗时由本地计时器每秒刷新。调用块固定为两行，`⎿` 后接进度或完成行。
- **等待指示**：替换 Pi 的 working message，有子代理在跑时显示 `✻ 等待 N 个子代理完成 · 耗时 · Esc 中断全部`。需要确认 Pi 扩展 API 是否支持设置 working message，否则用 patch。
- **页脚**：移除每个子代理一行的写法，只在有运行中的子代理时显示一行聚合信息；屏蔽 pi-subagents FleetView（通过 setWidget 覆盖或插件配置）。
- **Ctrl+O 展开**：`delegate` 显示 Markdown 报告和元信息行（类型、模式、完成原因、读取的文件）；其他回执也改为易读格式，原始 JSON 留给 `/export`。
- **给父模型的结果**：content 为 `summary`、一行元信息（是否部分结果、原因）和读取过的文件列表。receipt id 只留在 details 中，不进入模型上下文。

### 5.5 TUI 基础修复

- **T1 滚动模式**：方案 A，默认使用 `regular`（主屏 + 终端原生滚动，与 Claude Code 一致），可通过 `--fullscreen` 或配置 `tui.mode` 切回全屏。方案 B，保留全屏，但在新 assistant 消息开始和子代理完成时自动回到底部，并把提示汉化。推荐 A，先做 spike 验证 Pi `regular` 模式下编辑器、页脚、补全和 PTY 测试是否正常（见 §8）。
- **T2 文案汉化**：沿用现有的"源码哈希 + 精确 patch"机制，把 patch 扩展到 `tui-renderer.js`；能走扩展 API 的地方优先走 API。
- **T5**：`input` 处理函数按 `event.source` 区分来源。非 TUI 来源必须先 `inputs.accept` 拿到持久回执再继续，否则明确拒绝并记日志。绝不把原始异常直接显示给用户。补回归测试。
- **T7**：在 `session_start` 之前先安装一个占位页脚，或者在宿主就绪前隐藏 Pi 原生页脚。

### 5.6 提示词与工具描述

- `DELEGATION_PROMPT` 和 `delegate` 描述随阶段更新。阶段 2 起写明"彼此独立的调查放在同一回复里发出多个 delegate，它们会并行执行"；阶段 3 起说明后台、通知和 `wait_agents` 的用法。
- 保留现有约束：用户或项目禁止委派时不委派；简单查询由父代理直接处理；子代理的建议不是验收证据。

### 5.7 流式与工具输出

**SSE 修复（Python + Node）**

- **O1 扣留长度**：`emit_text` 改为只扣留"pending 文本中恰好是密钥前缀"的最长后缀；其余字符立即发出。安全性质不变：任何时刻已发出的文本都不可能拼出完整密钥。完整密钥一旦出现，仍按现有逻辑报错。需要补单测：密钥被切成任意两段或三段、密钥前缀之后跟了不匹配的字符、多字节字符。
- **O2 思考内容**：OpenAI 兼容流解析 `delta.reasoning_content`，作为 `{"type":"thinking_delta"}` 广播；Node `stream()` 推送 Pi 的 `thinking_start/thinking_delta/thinking_end` 事件。默认折叠，用 `setHiddenThinkingLabel('思考中…')` 显示标签，Ctrl+O 展开。思考内容只用于展示，不进入回执的结论字段。
- **O3 按调用过滤**：`subscribeModelDeltas(listener, generationId)` 按 `generation_id` 过滤。`stream()` 发出模型请求时取得本次调用的 `call_id`：需要宿主在 `model`/`child_model` 响应前先广播 `model_started{generation_id, permit?}`，或由 Node 在请求参数中带上 `call_id`。这是并行子代理（阶段 2）的前置条件。

**工具输出渲染（TS，优先走扩展 API）**

- 新建 `harness/pi/tool-render.mjs`。在 `workspaceTools`（project 模式）和 `directTools` 中，给 read/write/edit/bash/grep/find/ls 以及业务工具统一覆盖 `renderShell:'self'`、`renderCall`、`renderResult`：
  - 头部：`⏺ 工具名(主要参数)`。颜色由 `context.executionStarted` 和结果的 `isError` 决定：运行中灰色，成功绿色，失败红色。
  - 摘要规则：

    | 工具 | 折叠时的摘要 |
    |---|---|
    | read | `读取 N 行`（分页时注明范围） |
    | write | `已写入 N 行` |
    | edit | `已更新 +a −b`，附最多 8 行 diff（复用 Pi 的 `edit-diff.js`） |
    | grep/find | `找到 N 处 / N 个文件` |
    | ls | `N 项` |
    | bash | 运行中显示尾部 5 行；结束后显示 `耗时 · 退出码`，测试类命令保留尾部 |
    | 业务回执工具 | 状态和一句话原因 |

  - 错误统一为红色 `失败：…`，路径转换为项目相对路径。
  - Ctrl+O 展开后显示原始全文：文件内容、完整 diff、完整命令输出。回执类工具展开后显示易读字段，不显示 JSON。
- **探索合并（O7）**：连续的 read/grep/find/ls 合并成一条 `⏺ 浏览 … · 读取 … · 搜索 …`。Pi 每个调用一个组件，合并需要 patch `ToolExecutionComponent` 的挂载逻辑，或者让后续探索调用渲染为空、由第一个组件汇总。先做 spike，可以晚于其他项交付。
- **助手消息前缀 ⏺（O4）**：需要 patch assistant 消息组件。可选项，晚于工具渲染交付。
- **Working 行（O8）**：`setWorkingMessage` 动态显示 `执行中… (12s · ↓ 1.2k tokens · Esc 中断)`；有子代理时改为 §3.1 的"等待 N 个子代理"。tokens 取自 `model_delta` 计数和回执里的 usage。
- **文案（O9）**：renderer 文案在我们自己的 `tool-render.mjs` 中，天然是中文；`Tool output: expanded` 这类内置提示随 T2 的 patch 处理。

**流程约束**：渲染只改展示，不改工具语义、回执和发给模型的 content；改动后 `tools_sha256` 不应变化，需要回归确认。

---

## 6. 实施与验收核对（2026-11-11）

`[x]` 表示按本节注明的实现或替代设计交付，并有对应证据；`[ ]` 表示原要求的完整验收仍待补，不代表该功能没有实现。第 1–5 节保留原设计，不作为当前行为说明。

证据索引：当前树 `.validation/harness-npm-r1.log`（65 项 JS 及类型检查）、`.validation/harness-tui-unit-r1.log`（35 项 TUI 单元）；Python 分批运行边界见 [ACCEPTANCE](../../validation/tui/ACCEPTANCE.md)。当前并行 PTY 为 `.validation/subagent-parallel-final-r3/scenario.json`（8 项）；真实模型汇总为 `.validation/subagent-final-validation.json`。48 列 panel/stop 是旧树 `/Users/jerry/Desktop/KernelBridge/.validation/subagent-panel-pty-r5`、`subagent-stop-pty-r2` 的保留证据，不算新树重跑。

### P0：输入与流式

- [x] **P0-1** `host-actions.mjs` 用 `delivered` 防止 initial/native 重复交付；输入结算窗口按 follow-up 入队。`input-settlement.mjs` 验证恰好一次，旧树首帧立即提交 PTY 及当前 Esc 后继续输入通过；不把首帧触发测试写成固定 50ms 时限证明。
- [x] **P0-2** 使用独立 `codex/harness-20turn` worktree 验收；运行时源清单固定，不修改另一活跃 checkout。
- [x] **P0-3** `streaming.py` 只扣留可能构成密钥前缀的后缀。分段密钥、多字节及普通文字及时输出由测试覆盖；真实模型流式 PTY 通过，不宣称测量了端到端延迟指标。

### 阶段 1B：流式与工具输出

- [x] **1B-1** `tool-render.mjs` 统一状态头部、摘要与颜色，接入原工具显示，工具语义和回执不变；展示单元和当前 PTY 的工具行断言通过。
- [ ] **1B-2** read/write/edit/grep/find/ls/bash 摘要及相对错误路径已实现，有展示单元和选定工具 PTY 覆盖；原计划“每种工具成功、失败各一条 PTY 断言”的完整矩阵未逐项验收。
- [x] **1B-3** Ctrl+O 展开完整正文与易读回执；当前并行 PTY 验证报告无原始 JSON。
- [x] **1B-4** Working 显示耗时与 Esc，结束恢复。替代设计：没有可靠中途 usage 时不显示估算 tokens；子代理显示已结算的实际用量。
- [ ] **1B-5** `reasoning_content` → thinking 事件的解析、generation 隔离及完成消息已实现并通过协议测试；真实 DeepSeek thinking 模式的折叠/展开 PTY 尚未验收。默认非 thinking 路由的通过不能代替此项。
- [x] **1B-6** 连续探索调用合并，展示单元验证三项合为一行且边界正确。
- [x] **1B-7** 助手前缀与 Markdown 主题已接入受哈希校验的 Pi patch；真实模型 PTY 使用该显示路径。

### 阶段 1：展示与限额

- [x] **1-1** `delegate.description` 用作一行标题，旧回执回退到截断的 task；展示测试覆盖。
- [x] **1-2** 子代理进度包含当前工具、模型/工具次数、已结算用量与耗时；单元和两个同时运行的 PTY 断言通过。
- [x] **1-3** 收起显示完成摘要，展开显示 Markdown 报告；当前 Ctrl+O PTY 通过。
- [x] **1-4** 页脚聚合且不再挂载 FleetView；子代理全部结算后无存活行，展示单元和 PTY 覆盖。
- [x] **1-5** Working 显示等待数量；旧树 48 列三路 PTY 验证 3 → 2 → 1 与逐个通知。
- [x] **1-6** 父模型接收精简结果，原回执仍在 Journal；真实模型可引用子代理读取出的原始证据。
- [x] **1-7** 默认仅 `max_concurrent: 6`，超出排队；步数、工具数、时长、每轮与总数可显式配置。省略预算仍为无预算。配置和限额测试通过。
- [x] **1-8** `/agents` 列出类型与运行记录，可查看报告、Esc 返回；当前 PTY 覆盖操作，旧树 48 列 panel 证据覆盖窄屏及报告实时更新。
- [x] **1-9** 原生启动提示、Working、工具展开提示与滚动提示已本地化，KernelBridge 品牌和 patch 哈希拒绝机制保留；当前原生 Pi PTY 通过。
- [x] **1-10** 替代设计：产品状态未就绪时隐藏原生 token/预算页脚，就绪后显示产品页脚；不要求宿主尚未 ready 时显示伪造状态。旧树首帧 PTY 已断言不闪现原生页脚。

### 阶段 2：前台并行

- [x] **2-0** 保留社区 `pi-subagents@0.87.0`，并非 Pi 官方内置组件；多请求路径已通过真实 Pi 验收。
- [x] **2-1** 全为 delegate 的回复记录 delegation 批次并行执行，混合工具批次保持源顺序；两种分支均有测试。
- [x] **2-1b** 模型调用前分配 `generation_id`，受控流独立过滤。隔离测试还验证预览丢片段后以完整 effect 正文结束；当前 PTY 验证并发输出。
- [x] **2-2** Journal/网关原子准入、共享预算、并发上下文 CAS 已实现；定向测试覆盖并行支出后准入、实际限额和伪造上下文拒绝，真实并行最终无挂起预留。尚未补原计划的独立高并发 `propose` 压测，不能把这些边界测试称为压力测试。
- [x] **2-3** lane 内有序、lane 间并发，多 permit 独立结算。修正原设计：父回合只等待所属前台任务，后台任务不能成为父 `end_turn` 屏障；当前 PTY 在子任务被阻塞时验证父连续两条输入均完成。
- [x] **2-4** 宿主按 permit 派发子模型请求；当前 Journal 证明两子模型调用区间重叠。
- [x] **2-5** 多子任务乱序完成但结果顺序稳定；已知失败/取消局部结算，未知效应隔离父会话。定向单元和并行 PTY 覆盖。
- [x] **2-6** Esc 取消当前前台并行任务，结算后父输入可继续；后台任务由 `stop_agent` 或面板停止，不能把 Esc 宣称为无条件停止全部后台工作。未知效应仍保留。当前并行 PTY、旧树面板停止 PTY 通过。
- [x] **2-7** 自然任务自动委派、显式双子任务以及用户禁止委派均通过真实 DeepSeek 验收；证据跨两个会话，不宣称同一会话一次全部通过。

### 阶段 3：后台子代理

- [x] **3-1** 替代设计：由执行层 `background` 表、独立 lane 与插件并发槽管理，不新增 `ChildScheduler` 类。子代理仍运行时父输入及下一条输入均可完成，当前 PTY 已直接断言。
- [x] **3-2** 暴露 `delegate{background}`、`agents_status`、`stop_agent`。以事件通知代替阻塞式 `wait_agents`，文档与工具 schema 一致；停止作用于实际执行并保留原回执。
- [x] **3-3** 替代设计：完成通知使用 `kind: follow_up`、`initiator: agent` 和持久化子任务身份，不新增 notification 输入类型。空闲自动继续、忙碌排队；真实后台两个唯一通知及最终证据汇总通过。
- [x] **3-4** detach/恢复先结算已知效应，未知效应继续隔离；遗漏通知从原身份恢复，不重新执行子任务。恢复、通知去重、命令停止及 worktree 保留测试通过；旧树 panel PTY 验证 resume 不重复执行/通知。
- [x] **3-5** 完成条目与后台计数已实现；当前后台 PTY 与真实模型分别通过，不以等待文案代替真实运行证明。

### 阶段 4：可执行与可写子代理

- [x] **4-1** `kernel-tester` 可运行命令，仅前台并与父写入互斥；命令环境脱敏、输出限量及原进程停止由实执行测试覆盖。共享项目上的命令执行不等于操作系统只读沙箱。
- [x] **4-2** `kernel-worker` 在独立 Git worktree 修改并返回分支/diff，不自动合入父目录；要求 committed HEAD，快照不包含未跟踪文件。已完成 checkout 清理、提交保留、未知执行不清理均有测试。
- [x] **4-3** `.kernelbridge/agents/*.md` 定义在网关封存前加载；格式、限制及真实 Pi 子会话执行测试通过。

### 阶段 5：TUI 模式

- [x] **5-1** 已决定采用 `regular`；当前并行与真实模型 PTY 验证实际编辑器/页脚/中文/报告交互。完整 resize、补全及 fullscreen 组合仍沿用历史验收，不称本轮重新覆盖。
- [x] **5-2** 默认 `regular`，保留显式 `fullscreen`。

### 贯穿：验收与文档

- [x] **V-1** 当前并行 PTY 8 项通过。替代固定 1.6 倍墙钟阈值：用 Journal 调用区间重叠证明并行，用人为阻塞后台子任务时父两条输入已完成证明后台独立；同时验证 Ctrl+O、面板、完成通知及 Esc 后继续输入。
- [x] **V-2** 多 permit、结果顺序、取消、未知隔离、限额和预算边界的 Python 定向测试已通过；这里不包含 2-2 尚待补充的压力测试。分批运行和跳过项见 ACCEPTANCE。
- [x] **V-3** 真实 DeepSeek 的简单父任务、自动委派、显式双任务、禁止委派、双后台通知/汇总五项行为跨两个新会话通过，保留第一会话歧义断言失败记录。
- [ ] **V-4** 当前树 65 项 JS/TypeScript、35 项 TUI 单元、Python 分批检查及上述 PTY 已通过；原 `advanced_scenario.py`、完整 session resume/linked-worktree PTY 等历史矩阵尚未在本轮新树全量重跑，不宣称整个历史套件一次全绿。
- [x] **V-5** `docs/tui.md` 已记录当前行为，本表与 ACCEPTANCE 同步标明证据、替代设计及待验项；原失败记录保留。

---

## 7. 验收口径

- **并行**以 Journal 中子模型调用的时间区间重叠为准，不以界面观感为准。
- **展示**以 PTY 屏幕断言为准，覆盖 100 列和 48 列两种宽度。
- 子代理结论仍然只是建议，不构成正确性或设备证据；这一点在报告和文档里继续写明。
- 本地 fixture 和真实模型分开记录；不宣称与 Claude Code / Codex 的实现完全一致。

---

## 8. 决策（已定）

以下为当时的待决项，已按推荐方案实施：1 默认 `regular`；2 保留 pi-subagents；3 worktree 隔离；4 按 P0 → 1/1B → 2 → 3 → 4 → 5 实施；5 bash 按命令类别决定保留头部还是尾部。

### 原待决项

1. **TUI 默认模式**：`regular`（与 Claude Code 一致，使用终端原生滚动）还是保留 `fullscreen` 并补自动回到底部？推荐先做 5-1 spike，倾向 `regular`。
2. **子会话运行器**：继续用 pi-subagents，还是用 Pi SDK 自建？由 2-0 spike 给出依据。
3. **可写子代理的隔离**：共享工作区加互斥，还是 git worktree 隔离？推荐 worktree。
4. **实施顺序**：推荐 P0 → 阶段 1 与 1B（展示和输出对齐，可以并行）→ 阶段 2 → 阶段 3；阶段 4、5 视决策插入。阶段 1 和 1B 不改协议，可以最先交付可见改进。
5. **bash 折叠策略**：结束后保留尾部（失败信息通常在尾部，Pi 现状也是这样）还是像 Claude Code 一样保留头部？推荐按命令分类：测试和构建类保留尾部，其他保留头部。

---

## 9. 设计评审：不合理或不符合行业惯例之处

按"已在本轮修复 / 本轮未改但记录"区分。判断依据是常见 coding agent（Claude Code、Codex CLI）和一般分布式系统实践，不是逐项复刻。

### 9.1 本轮已修复

| # | 原设计 | 问题 | 处理 |
|---|---|---|---|
| D1 | 宿主连接读循环同步等待每个 effect 完成 | 一个慢请求会阻塞同一连接上的所有后续请求，并发子代理退化为串行；也是"单线程事件循环里做阻塞 I/O"的典型反模式 | effect 请求入队后立即继续读；结果按请求 id 回写（客户端本来就按 id 匹配） |
| D2 | 准入上下文（含 `call_id`）存放在共享的 store 字段 | 跨线程共享可变状态，并发准入会把别的请求的 `call_id` 写进日志 | 共享值 + 线程局部覆盖；子代理调用的溯源从 permit 推导，不依赖最后一次 `bind()` |
| D3 | 整条链路单通道严格串行（Pi 全局串行、单 RPC lane、单子 permit、单执行线程） | 互相独立的子任务无法并发，耗时随个数线性增长 | 只读委派批次独立记账（`execution_mode: "delegation"`），父 lane + 每个子代理一条有序 lane，子模型调用独立线程 |
| D4 | 流式输出固定扣留"密钥长度"个字符 | 正确性没问题，但延迟毫无必要；行业做法是只扣留可能构成密钥前缀的后缀 | 改为前缀匹配扣留 |
| D5 | 每个子代理默认限 6 次模型调用 / 16 次读取 / 120s，另有"每次回复个数"和"会话总数" | Claude Code 和 Codex 都不限单个子代理的步数、读取或时长（Codex 只限并发线程数和派生深度）；真实调查频繁触顶，只剩部分结果；也违背本产品"不设置即不限制"的原则 | 默认只保留 `max_concurrent`（6，超出排队）；其余上限改为可选；接近上下文上限时做最后一次总结 |
| D10 | 子代理类型写死在代码里 | 两家都用文件定义代理（Claude Code 用 Markdown frontmatter，Codex 用 TOML 角色） | `.kernelbridge/agents/*.md`，格式与 Claude Code 一致 |
| D11 | 全屏 alt-screen 会接管鼠标滚轮 | 滚动后不再跟随输出，复制需要按 Shift；Claude Code 默认主屏渲染 | 默认 `regular`，`fullscreen` 可选 |
| D6 | 工具结果展开即原始 JSON；工具头无状态 | 信息密度低，难以扫读；业界普遍是"状态头 + 一行摘要 + 展开查看全文" | 新的渲染层（§5.7） |
| D7 | 子代理状态分散在调用块、社区插件 FleetView、页脚三处 | 同一信息多处展示且口径不一 | 只在调用块内显示进度；页脚只在运行中显示一行计数；关闭 FleetView |
| D8 | 编辑器在宿主初始化完成前就可提交 | 启动竞态导致同一输入被提交两次（T5） | `initial()` 跳过已由编辑器投递的回执 |
| D9 | 改源码后必须手工逐个同步 `PIN_MANIFEST.json` 的哈希 | 仓库里没有对应工具，开发流程依赖临时脚本，容易漏改 | 新增 `source/scripts/update_manifest.py` |

### 9.2 记录但本轮不改

| # | 现状 | 建议 | 不改的原因 |
|---|---|---|---|
| R1 | 发给模型的委派结果是完整回执 JSON | 只给摘要和关键元信息，回执留在 details | 恢复与回执比对依赖该格式，需要单独设计迁移 |
| R2 | 每处理一个请求就广播一次全量 `status()`（扫描整个日志，含 Goal 用量） | 增量事件或节流（如 100ms 合并） | 会话规模变大后才明显；需要和 SDK 订阅语义一起改 |
| R3 | Pi 通过"按行号的精确补丁"定制 | 优先使用扩展 API；必要补丁随上游版本维护 | 现有 API 无法覆盖全部需求；补丁有哈希校验，失配时拒绝启动，风险可控 |
| R4 | 子代理依赖社区插件 pi-subagents 编排子会话 | 自建最小子会话运行器，消除插件 UI 泄漏和未用功能面 | 并发与后台已在插件之上实现；替换属于独立重构 |
| R5 | Python 承担模型 HTTP 与凭据 | 保持不变 | 凭据只留在宿主进程是有意的安全边界；语言迁移收益不抵风险（§4） |

---

## 附录：代码索引

| 主题 | 位置 |
|---|---|
| 全局串行工具执行 | `source/apps/agent/src/harness/pi/runtime-factory.mjs:81` |
| 单通道 RPC | `runtime-factory.mjs:103`，`source/apps/agent/src/clients/serial-rpc.ts` |
| 单子 permit | `source/apps/agent/src/loopx/model-control.ts:80` |
| 子代理授权与结算 | `source/packages/executor/src/kernelcat_executor/operations/authority.py:524-638` |
| 单 future 的委派效应 | `source/packages/executor/src/kernelcat_executor/capabilities/delegation.py:16,49` |
| 子代理步数、读取上限与指令 | `source/packages/executor/src/kernelcat_executor/operations/delegated.py` |
| 宿主连接循环与执行线程 | `source/apps/worker/src/kernelcat_worker/application_host.py:113,886` |
| 子代理状态投影 | `application_host.py:427` |
| 展示（调用块、结果、页脚） | `source/apps/agent/src/harness/pi/product-ui.mjs` |
| `/subagents` 文本 | `source/apps/agent/src/harness/pi/host-actions.mjs:413-424` |
| 输入回执错误 | `source/apps/agent/src/application/input-queue.mjs:18`，`host-actions.mjs:95` |
| 插件桥与 agent 定义 | `source/apps/agent/src/harness/pi/plugin.mjs` |
| 委派提示词 | `source/apps/agent/src/harness/pi/delegation-prompt.mjs` |
| 子代理配置默认值 | `source/apps/worker/src/kernelcat_worker/configuration.py:96-103` |
| Pi 全屏默认值 | `node_modules/@earendil-works/pi-coding-agent/dist/core/settings-defaults.js:61` |
| 跟随底部逻辑 | `node_modules/@earendil-works/pi-tui/dist/components/scroll-view.js` |
| Pi 并行批次判定 | `node_modules/@earendil-works/pi-agent-core/dist/agent-loop.js:373` |
| FleetView 小部件 | `node_modules/pi-subagents/src/tui/fleet-status.js:773` |
| 输入重复提交（T5） | patch 后的 `interactive-mode.host.js:922/944`，`host-actions.mjs` `initial()` / `submit()` |
| 流式扣留 / 思考内容 | `source/packages/executor/src/kernelcat_executor/capabilities/streaming.py` `emit_text`、`consume` |
| delta 订阅 | `source/apps/agent/src/application/worker.mjs:35`，`runtime-factory.mjs:134` |
| Pi 工具渲染扩展点 | `node_modules/@earendil-works/pi-coding-agent/dist/core/extensions/types.d.ts:97-110,366,488-537` |
| Pi 原生工具 renderer | `node_modules/@earendil-works/pi-coding-agent/dist/core/tools/renderers/*.js` |
| 同步输出 / 鼠标上报 | `node_modules/@earendil-works/pi-tui/dist/tui-alt-screen.js:12-22` |
