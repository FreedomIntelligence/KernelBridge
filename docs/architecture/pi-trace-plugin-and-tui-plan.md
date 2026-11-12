# Lattice Trace 做成 pi 插件，以及 TUI 待完善项：计划与 TODO

状态：§0 已完成（`18afa13`）；§A 第一版（A0–A6）已完成，在分支 `feat/pi-trace-plugin`；§B 的 D5、D9、D10、D12 已完成（D5、D12 来自分支 `feat/tui-d5-d12`，与插件分支合并后 `npm test` 79/79）；D11 等用户决定，B-N2 与第四阶段全量验收未做。日期 2026-11-11，2026-11-12 更新。

## 进展（2026-11-12）

- §0：另一个会话以 `7d474e3` 为共同祖先做了三方合并，Pi patch 用 patch builder 重建，`PIN_MANIFEST.json` 重新生成；`npm test` 62/62。未跟踪但被引用的 28 个文件一并提交。旧 worktree 和已合并分支已清理，磁盘可用空间 3.4 → 11.8 GiB。
- §A：共享核心抽到 `source/packages/pi-lattice-trace/`，KernelBridge 的 `/trace` 对同一 Journal 的投影和页面与重构前逐字节一致；插件、单元测试（7 项）、真实 Pi 端到端场景（6 项，含 headless Chrome）完成；`npm test` 74/74。
- §B：D5 用 Pi 的 `CustomEditor` 子类和 `ctx.ui.setEditorComponent` 实现空编辑器提示，不改 Pi patch；提示里不写 `@ 文件`，因为文件补全依赖的 `fd` 在这台机器上没有。D12 在 48/40/32/24/16 列下逐块渲染，修了 `/goal` 路径、窄宽 `/help`、32 列页脚分支名和 working 行换行四处，`validation/tui/layout.mjs` 5 项。
- A0 结论：
  1. pi 用 jiti 加载插件，`.ts` 相对导入和 `import.meta.url` 读取 `viewer/` 都可用，安装到 `node_modules` 下也一样。
  2. `message_end` 处理函数运行时，这条消息还没进 `getBranch()`，下一个事件才看得到；插件用 100 ms 去抖后再取。并行工具的 `tool_execution_end` 按完成顺序到达，toolResult 等整批结束后才按源顺序写入，所以运行中的工具要由插件自己记。
  3. `/new` 之后 pi 复用已加载的模块，只重新调用工厂函数，模块级单例保留；新会话的 `session_start` 会到达两次（新旧实例各一次），处理必须幂等。
  4. KernelBridge 用 `noExtensions: true` 加显式工厂列表加载 Pi，用户安装的插件不会进入 `kernelagent`。
  5. pi 的 `usage.input` 不含缓存命中：`input + cacheRead + cacheWrite` 等于 provider 的 `prompt_tokens`。assistant 的 `message.timestamp` 是请求开始时间，记录的 `timestamp` 是写入时间。
  6. pi 的压缩阈值是 `contextWindow − reserveTokens`（默认 16384），不是 80%。

文档分三部分：

- **§0 主目录工作区与 `main` 不同步**：阻塞后面所有工作，必须先处理。
- **§A Lattice Trace 做成独立 pi 插件**：`pi install` 之后就能用 `/trace`。
- **§B 当前 TUI 还要完善的地方**：`tui-polish-plan.md` 里没做完的项，加上这次检查新发现的问题。

工作量标记沿用 redesign 文档：S ≤ 半天，M 约 1 天，L 2 天以上。

---

## 0. 先决：主目录工作区与 `main` 不同步（P0，阻塞）

### 0.1 现状（2026-11-11 19:57 核实）

- `3a4d142`、`6dc9a79`、`9d98de8` 这三个提交是在 `/tmp/kbpolish-wt`、`/tmp/kbtrace-wt` 两个 worktree 里做的。之后 `main` 指针被直接前移（reflog 里两条 `reset: moving to HEAD`），主目录 `~/Desktop/KernelBridge` 的工作区没有跟着更新。
- 主目录有 33 个已修改的跟踪文件。它们的改动都在凌晨 02:00–02:56 之间，基线是 `7d474e3`（`7d474e3` 只改了 `PIN_MANIFEST.json` 和 `ACCEPTANCE.md`，所以对代码文件来说它和 `db57826` 一样）。按与 `HEAD` 的关系分两类：
  - **B 类，23 个**：只有未提交改动，后来的提交没碰过，可以直接保留。包括 `application_host.py`、`authority.py`、`commands.py`、`delegated.py`、`journal.py`、`client.mjs`、`worker.mjs`、`agents-panel.mjs`、operator pipeline 等。
  - **C 类，10 个**：既有未提交改动，后来的提交也改过，需要三方合并。它们是 `PIN_MANIFEST.json`、`docs/tui.md`、`patches/pi-coding-agent-1.5.0-host-actions.{json,patch}`、`host-actions.mjs`、`product-ui.mjs`、`runtime-factory.mjs`、`tool-render.mjs`、`validation/tui/subagent-presentation.mjs`、`validation/tui/subagent_live.py`。
- 另有 27 个未跟踪的新文件（`examples/scatter/*`、`operator-workflow-prompt.mjs`、若干 `validation/` 脚本）。
- **后果：**
  - C 类文件是旧版本加上未提交改动。如果直接提交，会删掉欢迎框、`elidePath`、`/trace` 的处理逻辑、`commandHelp` 两列表格，以及 `docs/tui.md` 里的 `/trace` 行和 “Layout and theme” 一节。
  - 现在 `host-actions.mjs` 导入的 `COMMAND_HELP`，在 `HEAD` 的 `command-policy.mjs` 里已经不存在了。模块加载直接报错（已用 `node -e "import(...)"` 复现）。
  - 主目录跑 `npm test`：51 项里通过 48 项，`compatibility.mjs`、`interaction.mjs`、`input-settlement.mjs` 三个测试文件都卡在这个导入错误上。`check:types` 能通过。
  - 主目录这份代码里的 TUI 加载 `host-actions.mjs` 时会失败。
- `/tmp/kbtrace-wt`（`feat/tui-polish-2`，位于 `9d98de8`）还有 5 个文件没提交：D9 排队计数和 D10 中断提示的初版，以及 `tui-polish-plan.md` 里关于 `Journal._transaction` 的性能发现（见 B-N2）。
- 数据卷 `/System/Volumes/Data` 已用 100%，只剩 3.4 GiB。PTY 验收、`npm install` 和 headless Chrome 都可能因此失败或超时。

### 0.2 TODO

- [x] **0-0** 确认没有别的会话还在改主目录（S）。Codex 的 `codex/harness-20turn` worktree 和另一个“仍活跃的验收 checkout”都要看一下。确认之前不要动主目录。
- [x] **0-1** 备份（S）。只读操作，输出写到 `/tmp`：
  ```sh
  cd ~/Desktop/KernelBridge
  FILES=$(git diff --name-only | grep -v '^PIN_MANIFEST.json$')
  git diff 7d474e3 -- $FILES > /tmp/kb-wip-20261111.patch      # 未提交改动，相对它真正的基线
  git ls-files --others --exclude-standard -z | xargs -0 tar czf /tmp/kb-wip-untracked-20261111.tgz
  ```
- [x] **0-2** 在独立 worktree 里把这些改动提交，再变基到 `main`（M）：
  ```sh
  git worktree add /tmp/kb-wip -b wip/redesign-followup 7d474e3
  cd /tmp/kb-wip && git apply /tmp/kb-wip-20261111.patch && tar xzf /tmp/kb-wip-untracked-20261111.tgz
  git add -A && git commit -m "Redesign follow-up: acceptance repairs from main checkout"
  git rebase main     # 冲突只应出现在 C 类那 9 个源码和文档文件
  ```
  合并原则：`HEAD` 的布局、`/trace`、`commandHelp`、`elidePath` 全部保留，未提交的行为修复（generation 过滤、agents 面板实时刷新、子代理耗时等）也全部保留。补丁的 `.json` 摘要按合并后的 `.patch` 重新计算。
- [x] **0-3** 用 `source/scripts/update_manifest.py` 重新生成 `PIN_MANIFEST.json`，不要手工合并（S）。
- [x] **0-4** 验证（S）：
  - `npm test` 51/51 通过；
  - `install.py --verify-only` 通过；
  - `./kernelagent` 能启动到空闲状态；
  - `node validation/tui/trace-projection.mjs` 通过。
- [x] **0-5** 把 `/tmp/kbtrace-wt` 里那 5 个文件提交到 `feat/tui-polish-2`，再合进 `main`（S）。这部分属于 §B 的 D9 和 D10，验收在 §B 完成。
- [x] **0-6** 主目录执行 `git checkout main && git reset --hard` 到合并后的 `main`（必须在 0-1 的备份确认之后）（S）。然后用 `git worktree remove` 删掉 `/tmp/kbpolish-wt`、`/tmp/kbtrace-wt`、`/tmp/kb-wip`。已合并的 `feat/*` 分支可以删除。
- [ ] **0-7** 腾出磁盘空间（S）。目标是剩余空间不少于 30 GiB，然后再做任何 PTY 验收。`.validation/` 里的旧 artifacts 先确认已经在结果索引里有 SHA256 记录，再清理。（部分：清掉 Kiro 残留、旧 Chrome 签名副本和可重建缓存后为 11.8 GiB；剩下的大头是微信、桌面归档和实验回执，等用户决定）
- [x] **0-8**（写进了仓库根目录的 `AGENTS.md`，`CLAUDE.md` 引用它） 约定（S）：以后在 worktree 里提交后，回到主目录用 `git merge --ff-only`，不再用 `update-ref` 直接移动 `main`。这一条写进 `CLAUDE.md` 或 `AGENTS.md`。

---

## A. Lattice Trace 做成独立 pi 插件

### A.1 目标与非目标

目标：

- 普通 pi 用户执行 `pi install <源>` 之后，输入 `/trace` 就能在浏览器里看到当前会话的实时轨迹：时间线、上下文、子代理泳道、压缩和文件溯源。
- 不依赖 KernelBridge 的宿主、Journal 或 CLI。
- 和 KernelBridge 的 `/trace` 共用同一份查看器和服务器核心，不分叉维护。

非目标：

- 不改 `lattice-viewer.html` 本身，仍然只用精确匹配的替换表来修改。
- 不在 KernelBridge 的 TUI worker 里加载这个插件。KernelBridge 的 `/trace` 继续由受信任的 CLI 进程提供，worker 不读 Journal。
- 不写会话文件，不通过 Web 界面控制 agent（比如 fork 按钮）。
- 不监听 127.0.0.1 以外的地址。

### A.2 架构

```
                    ┌─ journal-trace.ts  (KernelBridge Journal，已有)
lattice-viewer.html │
 + live-adapter.js ─┼─ server core: startTraceServer({ snapshot }) → { url, publish, close }
                    │      127.0.0.1 · 随机 token 路径 · Host 校验 · SSE · 精确替换表
                    └─ session-trace.ts  (pi SessionManager，新增)
```

数据从哪来：插件运行在 pi 进程内，所以直接用 `ctx.sessionManager.getBranch()` 和 `getHeader()` 从内存取当前会话，不去追读 JSONL 文件。这样的好处：

1. 临时会话（没有文件）也能看。
2. 不需要处理“最后一行只写了一半”的问题。
3. `/tree` 切换分支后，`getBranch()` 会直接给出新路径。

还没写进会话的正在执行的工具，由 `tool_execution_start` 和 `tool_execution_end` 事件维护一张 `running` 表来补。

包的目录 `source/packages/pi-lattice-trace/`：

```
package.json                 pi 清单、peerDependencies、files
extensions/trace.ts          插件入口：/trace 命令、事件、生命周期
src/server.ts                从 apps/agent/src/trace/server.ts 抽出的核心（只接受 snapshot）
src/viewer-page.ts           viewerPage() 和 TRANSFORMS
src/trace-core.ts            Trace/Step 类型、calibrate、applyWrite/applyEdit、toolTitle、clip
src/session-trace.ts         pi 会话记录 → kernelbridge.trace/v1
viewer/lattice-viewer.html   v15，sha256 与仓库里现有的一致
viewer/live-adapter.js       参数化之后的版本（见 A4）
test/                        单元、服务器、页面拼装测试和 fixtures
README.md  CHANGELOG.md
```

KernelBridge 的 `source/apps/agent/src/trace/` 改为从这个包导入核心，只保留 `journal-trace.ts`、`journalOf()` 和追读文件的代码（见 A1）。

### A.3 服务器核心的接口

```ts
startTraceServer(options: {
  snapshot: () => Trace;          // 每次 publish 时调用；纯函数，不做 IO
  title?: string; port?: number; host?: '127.0.0.1';
  labels?: { harness: string; source: string };   // 页面上的“KernelBridge · Pi”/“Journal（只读）”
}): Promise<{ url: string; port: number; publish(): void; close(): Promise<void> }>
```

- `publish()` 重新调用 `snapshot()`。只有 JSON 序列化结果和上次不同，才通过 SSE 推送。
- 只有在 `status === 'running'` 时，才每秒 publish 一次，用来推进运行中步骤的结束时间。
- KernelBridge 这一侧把现有的 `fs.watch` 加 1 秒轮询包成 `snapshot` 和 `publish` 调用，对外行为不变。

### A.4 pi 会话记录 → 轨迹步骤

取 `getBranch()` 给出的从根到叶的那条路径。时间零点是 `header.timestamp`，所有时间都换算成相对这个零点的秒数。

| pi 记录 | 轨迹步骤 | 说明 |
| --- | --- | --- |
| `SessionHeader` | `session.id/cwd/started_ns` | 父会话 `parentSession` 显示在元信息里 |
| `session_info` / `getSessionName()` | `session.title` | 没有名字时，用第一条用户消息的首行 |
| `message` · `system` | `config`：工具数、提示词各段 | 按顺序回放 `toolsAdded`/`toolsRemoved`；不单独生成步骤 |
| `message` · `user` | harness / `input` | `addTok` 先估算，再由 calibrate 校准 |
| `message` · `assistant` | model 步骤 | `tokIn = usage.input + cacheRead + cacheWrite`；`tokOut = usage.output`；`cacheReal = cacheRead`；`t1` 取消息时间戳，`t0` 取上一条记录的时间戳；`stopReason` 是 error 或 aborted 时标为 `error`；记录 `model` 和 `thinkingLevel` |
| assistant 里的 `toolCall` 块 + `toolResult` | tool 步骤 | 按 `toolCallId` 配对；`t0` 取 assistant 时间，`t1` 取 toolResult 时间；输出截断到 4,000 字符；`isError` 标为 `error`；没有结果但在 `running` 表里的是 `running`；两边都没有的是 `interrupted` |
| `bashExecution`（用户的 `!cmd`） | harness / `input`，命令 | 排除在上下文之外的 `!!` 不计 `addTok` |
| `custom_message` | harness / `input`（通知） | 带上 `customType` |
| `compaction` | harness / `compact` | `before = tokensBefore`，`after` 取下一次模型调用的 `tokIn`（没有就按摘要估算），附上摘要 |
| `branch_summary` | harness / `checkpoint` | “从另一分支返回”，附上摘要 |
| `model_change` / `thinking_level_change` | 零时长标记 | 标题写“切换模型 X”或“思考级别 Y” |
| `context_edit` | 零时长标记 | |
| `label` | 给目标步骤加标签 | |
| 成功的 `write` / `edit` 调用 | `commits` 和 `files` | 直接复用 `applyWrite`/`applyEdit`；pi 的 `edit` 参数格式是 `{path, edits:[{oldText,newText}]}` |
| `usage`、其它 `custom` | 忽略 | pi-subagents 的记录留到 A8 处理 |

- 步骤 ID：前缀加 pi 记录自带的 `id`。工具步骤用 `记录id/toolCallId`。会话变长时 ID 不变，“跟随最新一步”的逻辑可以照用。
- 上下文上限 `limit` 取 `ctx.model.contextWindow`。压缩阈值 `thresh` 从 pi 的 compaction 设置推出，读不到就用 80%。
- 用 `calibrate()` 把相邻两次模型调用之间的工具输出大小，按比例缩放到实际的 `tokIn` 差值上，与 KernelBridge 的做法相同。

### A.5 插件生命周期

- **服务器实例**放在 `globalThis[Symbol.for('pi-lattice-trace/server')]` 上，作为单例。pi 在 `/new`、`/resume`、`/fork` 时会重新加载插件。这样处理后，浏览器标签页通过 SSE 重连就能接着看新会话，URL 不变。
- **`session_start`**：用新的 `ctx` 重新绑定 `snapshot`，然后 publish。不要保留旧的 `sessionManager`，pi 文档明确说切换后旧对象不能再用。
- **`session_shutdown`**：`reason === 'quit'` 时关闭服务器；其它原因只解除绑定。
- **会触发刷新的事件**：`message_end`、`tool_execution_start`、`tool_execution_end`、`session_compact`、`session_tree`、`turn_end`。用 100 ms 去抖。
- **`/trace [--no-open] [--port N]`**：
  - `ctx.ui.notify` 显示 URL；
  - macOS 用 `pi.exec('open')` 打开浏览器，Linux 用 `xdg-open`；
  - 有 `SSH_CONNECTION` 时不打开浏览器，提示用 `ssh -L <port>:127.0.0.1:<port>` 转发；
  - 设置了 `PI_LATTICE_NO_BROWSER=1` 时也不打开浏览器。
- **非交互模式**（print、json、rpc）：只在交互模式注册命令。具体按 pi 文档的 “Mode Behavior” 一节在 A0 里核实。

### A.6 安全与隐私

- 沿用现有服务器的做法：只监听 127.0.0.1；随机 128 位 token 路径；校验 `Host` 头，防 DNS rebinding；只接受 GET；`no-store`、`nosniff`、`no-referrer`。
- 页面包含完整的工具输出和文件内容。URL 等同于凭证：README 里说明不要分享 URL，不要把它写进共享日志。
- 查看器会从 Google Fonts 加载字体，也就是有外部请求（因为设置了 `no-referrer`，不会带上本地 URL）。提供 `--offline` 选项：用精确替换去掉字体链接，回退到系统字体。

### A.7 分阶段 TODO

每一项写明“改什么”和“怎样算完成”。

**A0 Spike：先核实前提**（S）

- [x] **A0-1** pi 加载已安装包里的 `.ts` 插件时，能不能处理带 `.ts` 扩展名的相对导入，能不能用 `import.meta.url` 读到 `viewer/*.html`。完成标准：一个最小插件用 `node_modules/.bin/pi -e <dir>` 加载后，能读到 HTML 的长度。
- [x] **A0-2** 在 `message_end` 时，`getBranch()` 是否已经包含刚结束的 assistant 消息；并行工具执行时 toolResult 写入会话的时机。完成标准：记录事件顺序，写进本节。
- [x] **A0-3** 插件模块在 `/new` 等重载之后，`globalThis` 单例是否还在。
- [x] **A0-4** KernelBridge 冻结了资源集，确认用户全局安装的 pi 包不会被 KernelBridge 的 TUI 加载（所以不会出现两个 `/trace`）。

**A1 抽出共享核心**（M）：纯重构，KernelBridge 的行为不能变

- [x] **A1-1** 新建包目录，把 `lattice-viewer.html` 和 `live-adapter.js` 移进去。KernelBridge 改为从包里导入。
- [x] **A1-2** `server.ts` 拆成核心（接受 snapshot）和 KernelBridge 的文件追读层。
- [x] **A1-3** `journal-trace.ts` 里与数据源无关的函数（`calibrate`、`applyWrite`、`applyEdit`、`toolTitle`、`clip`、`firstLine`）移到 `trace-core.ts`。
- [x] **A1-4** 完成标准：
  - `node validation/tui/trace-projection.mjs` 5 项通过；
  - `trace_scenario.py` 5 项通过（需要先满足 0-7 的磁盘空间）；
  - `update_manifest.py` 更新 PIN；
  - `docs/trace.md` 改写“Viewer”一节里的路径。

**A2 投影 `session-trace.ts`**（M–L）

- [x] **A2-1** 按 A.4 的映射实现 `projectSession(entries, { header, running, model, now })`，纯函数。
- [x] **A2-2** 合成 fixtures，每种对应一个测试：
  - 单轮对话；
  - 同一条消息里有 3 个并行工具；
  - 工具出错；
  - 中断（有 toolCall 没有 toolResult）；
  - 压缩；
  - 切换模型和思考级别；
  - 用户的 `!cmd` 和 `!!cmd`；
  - write 之后 edit 的文件重建；
  - `/tree` 之后 `getBranch()` 只包含当前路径；
  - 追加记录之后 ID 不变；
  - 校准之后，每次模型调用的 `ctx` 等于下一次调用的 `tokIn`。
- [ ] **A2-3**（未做：本地没有可用的真实 pi 会话；KernelBridge 的 `state/pi-sessions/` 用 KernelBridge 的 provider 记录，留待下一轮）真实会话回放：KernelBridge 的 `state/pi-sessions/` 存的就是 pi 原生会话，可以挑 2–3 个本地跑一遍，确认不抛异常、步数合理。这些会话可能含私人内容，不提交；只提交一份脱敏后的 fixture。
- [x] **A2-4** 完成标准：全部单元测试通过，用 `node --test` 运行。

**A3 插件入口**（M）

- [x] **A3-1** 实现 `extensions/trace.ts`：A.5 的事件、单例、`/trace` 参数、打开浏览器、SSH 提示。
- [x] **A3-2** 完成标准：用 `PI_CODING_AGENT_DIR=$(mktemp -d)` 隔离出一个 pi 配置目录（不碰用户自己的 `~/.pi`），然后：
  - `node_modules/.bin/pi -e ./source/packages/pi-lattice-trace` 启动；
  - `/trace --no-open` 输出 URL，`trace.json` 能取到；
  - 再发一条提示后，SSE 的版本号会增加；
  - `/new` 之后同一个 URL 显示新会话；
  - `/quit` 之后端口关闭。

**A4 适配脚本参数化**（S）

- [x] **A4-1** `live-adapter.js` 里写死的“KernelBridge · Pi”“Journal（只读）”“由宿主准入与记账”“项目模式不快照工作区”这些文字，改为从 `T.config.labels` 读取。KernelBridge 一侧传入原来的文字。
- [x] **A4-2** pi 一侧配置区显示的内容：模型、思考级别、上下文上限、压缩阈值、工具数、已加载插件数。完成标准：页面上不出现 KernelBridge 字样；KernelBridge 的 `/trace` 页面与改动前逐字一致（快照测试）。（实际做法：适配脚本改了，页面字节随之变化；改为用 `trace_scenario.py` 的浏览器检查确认 KernelBridge 页面照旧渲染，投影 JSON 仍逐字节一致；“已加载插件数”没有做，pi 的插件上下文里拿不到这个数）

**A5 打包**（S）

- [x] **A5-1** `package.json`：
  - `name: pi-lattice-trace`，`type: module`，`version: 0.1.0`；
  - `keywords: ["pi-package"]`；
  - `pi.extensions: ["./extensions/trace.ts"]`；
  - `peerDependencies: {"@earendil-works/pi-coding-agent": "*"}`；
  - 没有运行时依赖，只用 Node 内置模块；
  - `files` 包含 `viewer/`、`src/`、`extensions/`。
- [x] **A5-2** 写 README（安装、使用、安全说明、截图）和 CHANGELOG（`## [0.1.0]`）。
- [x] **A5-3** 完成标准：在隔离的配置目录里 `pi install ./source/packages/pi-lattice-trace` 之后，`pi list` 能列出这个包；启动时不出现 “host-provided package in dependencies” 警告。

**A6 端到端验收**（M）

- [x] **A6-1** 复用 `validation/tui/` 里脚本化的 OpenAI 兼容 provider，在 PTY 里驱动原生 pi：读文件、并行工具、出错、压缩，然后 `/trace`。
- [x] **A6-2** 安装了 Playwright 时，用 headless Chrome 渲染页面，断言没有页面错误，时间线步数等于 `trace.json` 里的步数。
- [x] **A6-3** 完成标准：新脚本 `validation/tui/pi_plugin_trace_scenario.py` 全部检查通过，并把结果记进 `ACCEPTANCE.md`。

**A7–A10：第二版及以后**

- [x] **A7**（`findFork` 选最近的分叉点，最多 3 条分支；查看器 7 处 ID 比较改为按时间线排序；`trace_branches_render.py` 在 headless Chrome 里检查 9 个标签页 × 两个分支视角） 分支对比（M–L）：pi 的会话本身是树。用 `getTree()` 的每个叶子生成一个 `BR` 分支，适配脚本不再隐藏“分支 · 点选切换”。这正是 KernelBridge 一侧只能空着的那个视图。
- [ ] **A8** 子代理泳道（L）：
  - 通过 pi-subagents 的进程内 RPC 或状态查询，拿到每个子代理的 `sessionFile`；
  - 读取子会话，投影成 `M/cN` 泳道；
  - 父会话里的 `subagent` 工具调用作为 spawn 和 return 步骤。
- [x] **A9**（`bin/lattice-trace.ts`；`--offline` 和 `--otlp` 同样可用） 独立 CLI（S–M）：`pi-lattice-trace <session.jsonl>` 打开已保存的会话，从文件追读，复用 KernelBridge 的追读层。同时加上 A.6 说的 `--offline`。
- [ ] **A10** 发布决定（S，需要你决定）：
  - 发到 npm 还是 git；
  - 先确认 Lattice Trace v15 查看器的版权归属和许可证（Geist 字体是 OFL，通过 Google Fonts 链接引用，没有问题）；
  - 包名是否需要加 scope。

### A.8 风险

| 风险 | 应对 |
| --- | --- |
| 查看器内部函数（`loadSession`、`DS_INV`、`ISB` 等）被适配脚本直接依赖，换查看器版本就可能失效 | 保留“每个锚点必须恰好出现一次”的检查；查看器有变化时启动直接报错，不会悄悄渲染出示例数据 |
| pi 的 `usage` 在不同 provider 下语义不同（有的 `input` 已经包含缓存命中） | A2 先按 pi-ai 的约定（`totalTokens = input + output + cacheRead + cacheWrite`）实现；calibrate 只在估算部分上缩放；拿 Anthropic 和 OpenAI 兼容各一份真实会话核对 |
| 长会话每次都完整重新投影 | KernelBridge 实测 1,200 行约 4 ms；还是加上 100 ms 去抖；超过 1 万条记录时再考虑增量计算 |
| 插件 API 随 pi 版本变化 | `peerDependencies` 用 `*`，README 写明在 1.5.0 上验证过；A0 记录用到的全部 API |

---

## B. 当前 TUI 还要完善的地方

### B.0 这次检查做了什么，没做什么

- 读了 `docs/tui.md`、`docs/architecture/tui-polish-plan.md`、`tui-subagent-redesign.md` 和 `validation/tui/ACCEPTANCE.md`。
- 在主目录跑了 `npm test`：48/51，失败原因见 §0。
- 对比了三个 worktree 的状态。
- **没有跑 PTY 场景**：主目录的代码加载不起来，磁盘也满了，跑出来的结果说明不了问题。下面列的 TUI 呈现问题都来自已有的渲染截图和文档，不是这次新截的屏。§0 处理完之后，B-P4 会重新跑全套 PTY。

### B.1 已经完成的（不再列为 TODO）

- polish 计划第一阶段：D1 段落间距、D2 统一的 `/help` 表格、D3 `/goal` 状态块、D4 宿主反馈的标记、D6 工具行一定带主参数、D7 页脚隐藏零值、D8 页脚路径省略中间部分。
- Claude Code / Codex 式布局：欢迎框、`>`/`⏺` 前缀、working 行、两行页脚、`kernelbridge-dark` 和 `kernelbridge-light` 两个主题。
- 实时轨迹 `/trace` 和 `kernelagent trace`。
- 子代理重构：并行、后台、`/agents`、tester 和 worker、流式输出、思考内容折叠。

注意：以上全部只在 `main` 上，不在主目录的工作区里，见 §0。

### B.2 polish 计划里没做完的

| # | 内容 | 当前进度 | TODO 和完成标准 |
| --- | --- | --- | --- |
| D5 | 编辑器为空时显示占位提示，比如 `试试：修复 … · / 命令 · ! shell · @ 文件` | 没开始 | 按下第一个键就消失；`interaction_scenario.py` 断言空编辑器里有提示、输入一个字后提示消失；48 列下不换行（S） |
| D9 | 运行中显示排队的输入 | `kbtrace-wt` 里有未提交的初版：working 行显示 `N 条排队`，数字来自宿主的 `inputs[].status === 'queued'` | 合入（0-5）；PTY 里在运行中连续发两条消息，断言出现 `2 条排队`，然后依次减少；数字必须来自宿主回执，不能来自本地状态（S） |
| D10 | Esc 中断后在记录里留一行 | `kbtrace-wt` 里有未提交的初版：`已中断。工具调用的回执已保留，可以继续输入。` | 合入；改成计划里写的 `⏺ 已中断` 记录行，而不是浮在编辑器上方的反馈（会被下一条反馈覆盖）；`codegen_scenario.py` 断言中断后滚动记录里能看到这一行（S） |
| D11 | 计划和 todo 的展示 | 没有决定 | **需要你决定**，见 B.4 |
| D12 | 窄终端 | `/help`、`/goal` 和页脚已有 48 列检查 | 把 `/agents` 面板、子代理进度行、工具 diff、欢迎框也纳入 48 列和 24 行的检查（S–M） |
| P4 | 第四阶段验收 | 没做 | 先满足 0-7 的磁盘空间并让机器空闲，再跑全套 PTY（`pty`、`interaction`、`advanced`、`cancel`、`edges`、`resume`、`goalreview`、`codegen`、`trace`）；然后更新 PIN 和 `install.py --verify-only`，更新 `ACCEPTANCE.md` 和 `docs/tui.md`（M） |

### B.3 这次新发现的问题

- [x] **B-N1（P0）主目录工作区不同步**，见 §0。
- [x] **B-N2 `status` 的耗时随 Journal 长度增长**（M，涉及执行层改动，需要单独评审）：
  - `Journal._transaction` 每次调用都要拿排他 `flock`，并重新读取、校验整条哈希链；`status` 一次要做两次这样的事务。
  - 结果：会话越长，每个回合边界越慢；机器负载高时，`advanced_scenario` 会碰到 10 秒的 socket 超时。
  - 方案：在已经逐条校验事件的追加路径上，顺带维护一份内存里的投影状态，`status` 直接读它。Journal 仍然是权威来源，这份状态只是由写入方自己失效的缓存。
  - 完成标准：一万行 Journal 下，`status` 的耗时不随行数增长；`advanced_scenario` 在负载下不再超时。
- [x] **B-N3 `/trace` 里没数据的视图**（`/fork` 历史条目时，被放弃的回合显示为分支 B；工作区快照仍为空，因为项目模式不快照）（与 A7 合并考虑）：
  - KernelBridge 一侧的分支对比和工作区快照是空的。
  - Journal 里有 `transition_prepared` 记录的 `/fork`，可以把 fork 前后两段映射成两个分支（M）。
- [x] **B-N4 中断提示目前是“反馈”而不是“记录行”**：会被下一条反馈覆盖，滚动记录里也看不到。归入 D10。
- [ ] **B-N5 清理遗留的 worktree 和分支**（S）：
  - 已经合进 `main`、可以删除的 10 个（`git branch --merged main` 核实）：
    - `feat/live-trace`、`feat/tui-polish`、`feat/tui-polish-2`、`feat/tui-subagent-redesign`；
    - `codex/cann-portable-restore-fixes`、`codex/cann-task-sdk-20261109`、`codex/codegen-audit`、`codex/full-acceptance-20261109`、`codex/pi15-native-audit-20261109`、`codex/vector-cube-20turn`。
  - 只有 `codex/harness-20turn`（有自己的 worktree，位于 `8284906`）还没合并，先问清楚再动。
  - 已合并的在 0-6 里一并删除。

### B.4 需要你决定的

1. **D11 计划和 todo 的展示**。可选方案：
   - (a) 采用由宿主管理的计划工具，类似 Codex 的 `update_plan` 或 Claude Code 的 `TodoWrite`：有回执，记进 Journal，在 TUI 里显示成勾选清单，`/trace` 里也能看到。
   - (b) 不做，在 redesign 文档的“决策”一节写明原因。

   我的建议是 (a)，只给 Goal 模式和长任务开启。理由：Goal 会连续执行很多回合，用户现在只能从工具行去猜进度；而且同一份计划可以直接进 `/trace` 的时间线。
2. **A10 发布方式**：插件是只放在仓库里自用，还是发到 npm 或 git 给别人用。这决定 A5 要不要做 npm 发布前的检查（`npm pack` 预检、包名、许可证）。
3. **主目录里 B 类那 23 个未提交改动的归属**：从内容看，是子代理重构验收之后的后续修复（并发预算 CAS、generation 误认、首帧页脚、中断收尾等，redesign 文档的改动里列了这些）。需要确认它们来自哪个会话、是否已经验证过，然后再按 0-2 提交。

### B.5 已知限制（文档已经声明，不作为本轮 TODO）

- 后台子代理随交互 worker 一起结束，记为中断。
- `kernel-worker` 不复制未跟踪的文件，只快照已跟踪的改动。
- 没有做 Linux、NPU 设备或跨主机的验证。
- 思考内容只在 DeepSeek 上实测过。
- working 行不显示 token 数，因为流式期间没有可靠的计数。

---

## 执行顺序

```
0-0 → 0-1 → 0-2/0-3 → 0-4 → 0-5 → 0-6 → 0-7       （先恢复一个干净、可启动的 main）
  ├─ B: D9/D10 收尾 → D5 → D12 → B-N2（单独评审）→ P4 验收
  └─ A: A0 → A1 → A2 → A3 → A4 → A5 → A6   （v1）→ A7/A8/A9（v2）→ A10
```

A1 会改 `src/trace/` 和 PIN；B 的改动集中在 `harness/pi/`。两条线可以在不同的 worktree 里并行做，但必须按 0-8 的约定合回 `main`。
