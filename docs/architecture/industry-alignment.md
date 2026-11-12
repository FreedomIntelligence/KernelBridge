# 对齐行业最佳实践：计划工具、Journal 读路径、TUI 验收、轨迹格式

状态：调研完成；§2 已实施并经独立审查（`3a2d5cb` 及其修复）；§1 已实施（P-4 只做了续跑注入，压缩后的注入未做）；§3、§4 待实施。均在分支 `feat/journal-read-path`。日期 2026-11-12。

上一轮剩下三件事：D11 计划与 todo 的展示、`status` 每次重读整份 Journal、全套 PTY 验收。这次按题目分头调研了主流产品和规范的做法，再加上 trace 插件相关的轨迹格式标准。每条结论都给出处；只有搜索摘要、没能读到原文的地方单独注明。

---

## 1. 计划与 todo（D11）

### 业界现状

| 产品 | 工具 | 结构与状态 | 约束 | 默认 |
| --- | --- | --- | --- | --- |
| Codex CLI | `update_plan` | `{explanation?, plan:[{step, status}]}`，每次整份替换；`pending / in_progress / completed` | “最多一个 in_progress”只写在说明里，处理函数不校验 | 2026-08-31 起需配置 `tools.update_plan.enabled` 才开启（[PR #41744](https://github.com/openai/codex/pull/41744)） |
| Claude Code | `TaskCreate / TaskUpdate / …`（原 `TodoWrite`） | `pending / in_progress / completed / deleted`，可带依赖 | `TaskCompleted` hook 可阻止完成 | 新模型默认关闭，后台和云端会话保留（[工具说明](https://code.claude.com/docs/en/tools-reference#task-tool-availability)） |
| Gemini CLI | `write_todos` | 加 `cancelled / blocked` | 校验器拒绝多于一个 in_progress | 默认开启（[文档](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/todos.md)） |
| Cline | Focus Chain | Markdown 清单 | 每 6 条消息提醒一次 | 已弃用（[说明](https://github.com/cline/cline/blob/main/docs/resources/deprecations.mdx)） |
| Amp | todo 工具 | — | — | 2026 年 1 月移除，理由是“花时间也花 token”（[文章](https://ampcode.com/news/todos-are-done)） |

要点：

- 趋势是收缩：常开的 todo 工具在新模型上收益不明显，工具定义和提醒还要占上下文。仍然保留的场景是长时间、无人值守的会话（Claude Code 的后台和云端会话）。
- 计划默认只是建议，不卡完成。Codex 的 Goal 续跑模板明确写着“没执行的计划不算进展”“不要用更新计划代替干活”（[continuation.md](https://github.com/openai/codex/blob/main/codex-rs/ext/goal/templates/goals/continuation.md)）。
- 有用的证据都是定性的：Manus 把 todo 反复写回上下文末尾，用来减少长任务里的跑偏；Anthropic 的长任务框架用 JSON 功能清单防止过早宣布完成，模型只能改 `passes` 字段（[文章](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)）。没有找到“todo 工具提升任务成功率”的公开评测。

### 决定

只在 Goal 模式提供 `update_plan`，沿用 Codex 的结构，由宿主校验和记账。

1. **结构**：`{explanation?, plan:[{step, status}]}`，每次整份替换；状态 `pending / in_progress / completed`。
2. **宿主校验**（Codex 没做，这里补上）：最多一个 `in_progress`；步骤不能为空；最多 12 步、每步最多 80 字（这两个上限是我们自己定的）。违反时返回工具错误。
3. **只在 Goal 激活时出现**，和 `update_goal` 一样动态增减；子代理不给，计划归父代理所有。
4. **记进 Journal**：每次接受的 `update_plan` 调用本身就有准入和结算回执，回执里带完整计划、说明和 `goal_id`，不另写事件（和 Pi 示例插件、Codex 一致）。当前计划就是本 Goal 最后一次成功的回执。续跑和压缩之后，把当前计划放回上下文，不另加提醒。
5. **不拿计划卡完成**：最终计划交给新开的完成审查器，当成核对清单；没完成或悄悄删掉的步骤要在审查里标出来。只更新计划、没有其他工具调用的回合不算进展。
6. **显示**：TUI 里每次更新显示一个计划块，已完成的打勾并划线，进行中的高亮，未开始的变暗，末尾写 `n/m`；页脚加 `计划 n/m`。`/trace` 时间线上显示计划变化和说明。
7. **提示词**（改写自 Codex）：三步以上的多阶段工作才用；步骤短、可核对；完成一步再开始下一步，可以一次标多个完成；计划变了写 `explanation`；不要复述计划。

### TODO

- [x] **P-1** 宿主工具 `update_plan`：结构、校验、Journal 记录、只在 Goal 激活时开放。单元测试覆盖：合法更新、两个进行中、空步骤、超长、Goal 未激活时不可用。
- [x] **P-2** 状态投影 `state["plan"]`（最新计划和历史），`status` 返回给 TUI。
- [x] **P-3** TUI：计划块渲染和页脚 `计划 n/m`。48、40、32 列的布局检查。
- [ ] **P-4**（部分：续跑注入和审查器输入已做；压缩后的注入未做）续跑和压缩时注入当前计划；完成审查器的输入里加最终计划，审查结论里标出未完成的步骤。
- [x] **P-5** “只更新计划”的回合不计入进展（和只调 `get_goal` 同等对待）。
- [x] **P-6** `/trace`：KernelBridge 的 Journal 投影里加计划步骤（harness / `plan`）。
- [x] **P-7** 一个 PTY 场景：Goal 激活后更新计划、渲染、完成审查读到计划；非 Goal 模式下工具不存在。

---

## 2. Journal 读路径（B-N2）—— 已实施

### 业界做法

- **读模型和检查点**：事件流是唯一权威，读模型从某个位置开始只追新事件（KurrentDB、Akka Projection、Marten 的 high water mark，Fowler 的 Event Sourcing）。
- **只校验新增部分**：Certificate Transparency（RFC 9162）、Trillian、Rekor 都是“从一个已信任的树头出发，只证明新的部分是旧部分的延续”；全量检查单独做（如 `git fsck`）。
- **多进程文件**：读者用共享锁、写者用排他锁；用设备号、inode 和大小判断截断或被替换（Filebeat 的做法）。
- **同类产品**：Codex、Pi、Claude Code 的会话文件都是启动时读一遍，之后在内存里追加，都不会每一轮重读整份文件。

这一份调研的网页抓取大多被拦，出处来自搜索摘要，见提交说明里的链接清单。

### 我们怎么做的

- 每个进程缓存一份已校验的前缀：文件身份、字节偏移、最后一行的起点和哈希、行数、一份 `marshal` 快照。每次事务只校验偏移之后的新内容，并重新计算最后一行的哈希作为锚点。
- 已校验的历史只能往后接：文件变短、锚点变了、替换后的文件不是原历史的延续，都报完整性错误。
- 全量校验在首次读取、文件被替换、以及每隔 300 秒时做。
- 每次交给调用方的都是全新对象，调用方怎么改都影响不到缓存。
- 只读事务不能追加。所有事务都拿排他锁：起初只读事务用的是共享锁，独立审查发现 `flock` 不给排队的写者优先权，6 个进程不停读时一次准入等了 23.5 秒；有了缓存，排他读也很短。
- 本进程自己的追加在 `fsync` 成功后并入已校验前缀，所以文件被一份旧副本替换、丢掉了自己刚写的行，也能发现。
- `status` 由 4 次事务（加上 `goal_usage` 的第 5 次）合成 1 次。
- 实测（同一份 1,305 行、10 MB 的 Journal，负载约 22）：每次快照 336 ms → 43 ms；只取行 21 ms。

### 取舍

- **漏检窗口**：已校验前缀内部被改动，只要文件总大小和最后一行都没变，要到下一次全量校验（最多 300 秒）或重启才会发现。原来是下一次读取就能发现。
- **新增的检测能力**：有人重算整条哈希链改写历史，现在当场就会发现；原来查不出来。

### 后续

- [ ] **J-1** macOS 上用 `F_FULLFSYNC` 代替 `fsync`。macOS 的 `fsync` 不刷磁盘缓存（Apple 文档）。这会让每次写入变慢，需要先单独测一下影响。
- [ ] **J-2** `status` 拿不到共享锁时（等满 2 秒），返回带 `as_of_seq` 和 `stale: true` 的已提交状态，而不是一直等。
- [ ] **J-3** 如果 Journal 继续增长，把 `_state` 和 `project` 改成增量折叠；现在占比很小（0.3–4 ms），暂不需要。

---

## 3. TUI 验收（P4）

### 业界做法

- **Codex**：可见的界面改动都要有快照测试（`insta` + `vt100`，同一个快照里放 80×24 和 40×16 两种尺寸）；流式测试由测试代码逐块放行；真实二进制的 tmux 测试默认跳过，手动跑；nextest 失败重试一次，容易争抢资源的测试串行跑，配置里写着“并发高了会在资源争抢下超时”（[nextest.toml](https://github.com/openai/codex/blob/main/codex-rs/.config/nextest.toml)）。
- **Pi 自己**：pi-tui 用 `@xterm/headless` 做虚拟终端，有中日韩字符在边界处的回归测试。
- **Anthropic**：Claude Code 重写渲染器时，用随机属性测试（随机宽度、内容长度、Unicode 边界情况）对比新旧渲染器在 xterm.js 里的输出。
- **Textual、Bubble Tea、@microsoft/tui-test**：固定终端尺寸、按条件等待而不是 sleep、失败时保存可回放的记录。
- **结果分类**：通过、不稳定、失败要分开报告（Playwright、nextest）；有不稳定的就不算整轮通过；隔离出去的测试要有负责人和期限（Fowler）。
- **我们的模拟器**：pyte 0.8.2 不处理备用屏幕（`?1049`）和同步输出（`?2026`），而 pi-tui 两者都会发。

### 差距与 TODO

- [x] **T-1** 统一超时倍数 `KB_TIMEOUT_SCALE`，作用于所有等待和那个 10 秒的 socket 超时；默认值按“负载 ÷ CPU 数”算，写进结果。
- [x] **T-2**（部分：超过基础时限打 `SLOW`，超时信息里写距上次终端输出多久；没有按“无进展”提前判定挂住，避免误杀正常空闲等待）判断挂住看有没有进展：一段时间内既没有新的 PTY 输出，也没有新的模型请求或 Journal 行，才算挂住；只要还有进展，就允许用满放大后的时间。超过基础时间先打 `SLOW`，到硬上限再终止。
- [x] **T-3** `run.py` 的结果分四类：通过、不稳定、失败、环境问题。只在超时之后重试，最多一次，断言失败不重试。有不稳定就不算整轮通过。前后各记录一次负载；负载过高时的结果标成“环境退化”。`ACCEPTANCE.md` 现在“环境超时不算通过”的规则落成 `acceptance.json` 的字段。
- [x] **T-4** 默认串行（已经是 `--jobs 1`）；每个场景独立的 HOME、socket 和端口。
- [ ] **T-5** 失败时保存：带时间戳的 asciicast v2 转录、最后几屏、进程树、负载采样、模型请求日志，按 SHA-256 写进 `acceptance.json`。
- [ ] **T-6** 加一个确定性开关：spinner 用空帧、冻结耗时显示，固定 `TERM` 和配色。
- [ ] **T-7** 布局检查下沉到 Node：在 32、40、48、80、110 列下渲染整屏到 `@xterm/headless`，生成快照文件，只有显式加标志才更新。PTY 场景只保留必须用真进程的部分：启动、按键和粘贴、调整窗口大小、退出恢复、attach 和 resume、进程组清理、模型调用次数。
- [ ] **T-8** 宽度和中日韩字符的随机属性测试：每行可见宽度不超过列数，双宽字符不被切开。
- [ ] **T-9** 在 T-1 到 T-3 完成、机器空闲时跑一次全套 PTY，按新格式记录验收结果。

本轮已经修了 `main` 上几处过时的断言（见 `3a2d5cb`）。现在这几个场景都能在负载 20 左右的机器上通过：`subagent_parallel`、`subagent_stop`、`subagent_panel`、`interaction`、`codegen_edges`、`trace`。

---

## 4. 轨迹格式（`/trace` 和 pi 插件）

### 业界做法

- **标准是 OpenTelemetry 的 GenAI 语义约定**，但全部还在 “Development” 阶段，没有 stable 的部分（[spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md)）：
  - span 类型：`chat {model}`、`execute_tool {tool}`、`invoke_agent {name}`；
  - token 字段：`gen_ai.usage.input_tokens` 要包含缓存命中；缓存读、缓存写、推理 token 各有单独的字段。
- **内容默认不导出**：提示词、输出、工具参数和结果都要单独开启。
- **各家都默认关闭导出**：Claude Code 的遥测需要主动开启，提示词和工具内容另外三个开关；Codex 的 `[otel]` 默认关；Gemini CLI 默认关。
- **查看工具**：Phoenix（OpenInference）、Langfuse、LangSmith 都能直接接收 OTLP。
- **没有现成约定的部分**，各家用自己的命名空间：上下文压缩前后的 token 数、子代理之间的关系、文件 diff。

### 对应关系

| 我们的步骤 | OpenTelemetry GenAI | OpenInference |
| --- | --- | --- |
| 会话 | 所有 span 上的 `gen_ai.conversation.id` | `session.id` |
| 模型步骤 | `chat {model}`，带 usage 和 `finish_reasons` | LLM |
| 工具步骤 | `execute_tool {tool}`，带 `gen_ai.tool.call.id` | TOOL |
| 子代理泳道 | `invoke_agent {name}` 子 span | AGENT |
| 上下文压缩 | 下一个模型 span 上的 `gen_ai.conversation.compacted`；前后 token 数用 `kernelbridge.compaction.*` | — |
| 文件写入和修改 | 只有 `execute_tool`，diff 属于受控内容 | TOOL |
| fork、/new | 没有约定 | — |

### 我们已经对齐的

- `tokIn` 已经包含缓存命中，和标准一致。Claude Code 的导出不包含，需要使用者自己换算，我们没有这个问题。
- 本地查看器只监听 127.0.0.1、用随机路径令牌、校验 `Host` 头，和 MCP 对本地服务器的要求、OTel Collector 的默认配置一致。

### TODO

- [ ] **O-1** 步骤模型补 `cacheWrite` 和 `reasoningOut`；截断的输出加 `truncated` 和 `original_length`。
- [ ] **O-2** 可选的 OTLP/HTTP 导出，默认关闭，用标准的 `OTEL_EXPORTER_OTLP_*` 环境变量；同时写 OpenTelemetry 的 span 名和 `openinference.span.kind`；在轨迹里记录所用约定的版本。
- [ ] **O-3** 导出默认不带内容；提示词、工具参数、工具输出三个独立开关；被隐去的写成显式占位，而不是空值。
- [ ] **O-4** 把 `TRACEPARENT` 传给工具子进程，让它们的 span 能接到我们的轨迹上。

---

## 执行顺序

1. §2 合并前：等独立审查结论，处理发现的问题，再合入 `main`。
2. §1 P-1 到 P-7：Goal 模式的 `update_plan`。
3. §3 T-1 到 T-6，然后 T-9 全套验收；T-7、T-8 放在下一轮。
4. §4 O-1 到 O-4。
