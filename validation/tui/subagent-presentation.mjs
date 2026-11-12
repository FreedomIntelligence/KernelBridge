import test from 'node:test';
import assert from 'node:assert/strict';
import { installProductUI, subagentStatus, workingMessage, welcomeBox, elidePath, queuedInputs } from '../../source/apps/agent/src/harness/pi/product-ui.mjs';
import { commandHelp } from '../../source/apps/agent/src/harness/pi/command-policy.mjs';
import { initTheme } from '../../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js';
import { visibleWidth } from '../../source/apps/agent/node_modules/@earendil-works/pi-tui/dist/index.js';
import { directRenderers, withProductRenderers, updateSubagentProgress, displayPath, formatDuration } from '../../source/apps/agent/src/harness/pi/tool-render.mjs';

initTheme();
const theme = { fg: (_color, value) => value, bold: value => value, bg: (_color, value) => value };
const context = (extra = {}) => ({ cwd: '/repo', state: {}, isPartial: false, isError: false, expanded: false,
  executionStarted: true, toolCallId: 'kc_1', invalidate() {}, ...extra });
const screen = component => component.render(80).join('\n');

test('footer shows one live child line and nothing once children settle', () => {
  let footer;
  const ui = { ui: { setHeader() {}, setFooter(factory) { footer = factory({}, theme, { getGitBranch() {} }); } } };
  const state = { workspace_mode: 'project', model: 'fixture', subagents: [
    { agent: 'kernel-scout', status: 'running', steps: 2, tool_calls: 1, task: '梳理开发流程'.repeat(30) },
    { agent: 'kernel-reviewer', status: 'completed', steps: 3, tool_calls: 4, task: 'done' },
  ] };
  installProductUI(ui, () => state);
  // The footer carries persistent status; the working row above the editor is
  // what says the turn is waiting on children, so the two do not repeat.
  assert(footer.render(48).some(line => line.includes('子代理 1 个运行中')));
  assert(!footer.render(48).some(line => line.includes('等待 1 个子代理完成')));
  state.subagents[0].status = 'completed';
  assert(!footer.render(48).some(line => line.includes('子代理')));
  assert.match(workingMessage({ subagents: [{ status: 'running' }, { status: 'running' }] }, Date.now() - 65000), /等待 2 个子代理完成… \(1m05s · esc 中断\)/);
});

test('delegate row shows title, live progress, completion metrics and an explicit partial result', () => {
  const { renderCall, renderResult } = directRenderers('delegate');
  const args = { agent: 'kernel-scout', description: '梳理开发流程', task: 'inspect\x1b[2Jcode '.repeat(20) };
  updateSubagentProgress([{ tool_call_id: 'kc_1', status: 'running', steps: 2, tool_calls: 3, tokens: 1500,
    latest_tool: 'search_text', latest_detail: 'def build' }]);
  const running = screen(renderCall(args, theme, context({ isPartial: true })));
  assert.match(running, /⏺ kernel-scout\(梳理开发流程\)/);
  assert.match(running, /⎿  搜索 def build · 工具 3 次 · 模型 2 · 1\.5k tokens/);
  assert(!running.includes('\x1b') && !running.includes('inspect'));
  const fallback = screen(renderCall({ agent: 'kernel-reviewer', task: '梳理验证流程：'.repeat(20) }, theme, context({ isPartial: true })));
  assert.equal(fallback.split('\n').filter(line => line.includes('⏺')).length, 1);
  assert(fallback.split('\n')[0].includes('…'));
  const result = { isError: false, details: { outcome: { status: 'completed', data: {
    agent: 'kernel-reviewer', partial: true, completion_reason: 'step_limit', steps: 6, tool_calls: 5,
    summary: 'Known evidence; **unresolved** question remains.' } } } };
  const collapsed = screen(renderResult(result, { expanded: false }, theme, context({ durationMs: 42000 })));
  assert.match(collapsed, /部分结果（模型调用上限） · 工具 5 次 · 模型 6 · 1\.5k tokens · 42s/);
  assert.doesNotMatch(collapsed, /unresolved/);
  const expanded = screen(renderResult(result, { expanded: true }, theme, context({ durationMs: 42000 })));
  assert.match(expanded, /unresolved question remains/);
  assert.doesNotMatch(expanded, /"outcome"|\{"/);
  assert.match(subagentStatus({ agent: 'kernel-scout', description: '梳理', status: 'failed', reason: 'timeout', steps: 1, tool_calls: 0 }), /kernel-scout · 失败：超时 · 工具 0 次 · 模型 1 · 梳理/);
});

test('native tools render a status header and one-line summaries', () => {
  const tool = name => withProductRenderers({ name, execute() {} });
  const read = tool('read');
  assert.match(screen(read.renderCall({ path: '/repo/src/a.py' }, theme, context())), /⏺ Read\(src\/a\.py\)/);
  assert.match(screen(read.renderResult({ content: [{ type: 'text', text: 'a\nb\nc' }] }, { expanded: false }, theme, context())), /⎿  读取 3 行/);
  const missing = read.renderResult({ content: [{ type: 'text', text: "ENOENT: no such file or directory, access '/repo/missing.py'" }] },
    { expanded: false }, theme, context({ isError: true }));
  assert.match(screen(missing), /失败：ENOENT: no such file or directory, access 'missing\.py'/);
  const bash = tool('bash');
  const output = Array.from({ length: 30 }, (_, index) => `line ${index + 1}`).join('\n');
  const test = screen(bash.renderResult({ content: [{ type: 'text', text: output }] }, { expanded: false }, theme,
    context({ args: { command: 'npm test' }, durationMs: 2300 })));
  assert.match(test, /另有 25 行/); assert.match(test, /line 30/); assert.doesNotMatch(test, /line 1\n/); assert.match(test, /2\.3s · 退出码 0/);
  const listing = screen(bash.renderResult({ content: [{ type: 'text', text: output + '\n\nCommand exited with code 2' }] }, { expanded: false }, theme,
    context({ args: { command: 'ls -la' }, isError: true, durationMs: 100 })));
  assert.match(listing, /失败：line 1/);
  const listingOk = screen(bash.renderResult({ content: [{ type: 'text', text: output }] }, { expanded: false }, theme,
    context({ args: { command: 'ls -la' }, durationMs: 100 })));
  assert.match(listingOk, /line 1\n/); assert.doesNotMatch(listingOk, /line 30/);
  const running = screen(bash.renderResult({ content: [{ type: 'text', text: 'tick 1\ntick 2' }] }, { expanded: false, isPartial: true }, theme,
    context({ args: { command: 'sleep' }, state: { startedAt: Date.now() - 3000 } })));
  assert.match(running, /tick 2/); assert.match(running, /运行中 · 3\.0s/);
  const edit = tool('edit');
  const diff = ' 1 def add(a, b):\n-2     return a - b\n+2     return a + b';
  assert.match(screen(edit.renderResult({ content: [{ type: 'text', text: 'ok' }], details: { diff } }, { expanded: false }, theme,
    context({ args: { path: 'solution.py' } }))), /已更新 solution\.py：\+1 −1/);
  const write = tool('write');
  assert.match(screen(write.renderResult({ content: [] }, { expanded: false }, theme, context({ args: { path: 'n.md', content: 'a\nb\n' } }))), /已写入 2 行/);
  assert.match(screen(tool('grep').renderResult({ content: [{ type: 'text', text: 'No matches found' }] }, { expanded: false }, theme, context())), /未找到匹配/);
  assert.equal(displayPath('/elsewhere/x', '/repo'), '/elsewhere/x');
  assert.equal(formatDuration(125000), '2m05s');
  assert(screen(read.renderCall({ path: 'x' }, theme, context())).split('\n').every(line => line.length <= 80));
});

test('consecutive exploration calls collapse into one row until a boundary', async () => {
  const { explorationBoundary } = await import('../../source/apps/agent/src/harness/pi/tool-render.mjs');
  explorationBoundary();
  const tool = name => withProductRenderers({ name, execute() {} });
  const rows = [['read', 'g1'], ['read', 'g2'], ['grep', 'g3']].map(([name, id]) => ({ name, id,
    call: tool(name).renderCall({ path: 'a.py', pattern: 'x' }, theme, context({ toolCallId: id })) }));
  assert.match(screen(rows[0].call), /⏺ 浏览 读取 2 个文件 · 搜索 1 次/);
  assert.equal(rows[1].call.render(80).length, 0);
  assert.equal(rows[2].call.render(80).length, 0);
  const expanded = tool('read').renderCall({ path: 'a.py' }, theme, context({ toolCallId: 'g2', expanded: true }));
  assert.match(screen(expanded), /⏺ Read\(a\.py\)/);
  explorationBoundary();
  const after = tool('read').renderCall({ path: 'b.py' }, theme, context({ toolCallId: 'g4' }));
  assert.match(screen(after), /⏺ Read\(b\.py\)/);
  const bash = tool('bash').renderCall({ command: 'ls' }, theme, context({ toolCallId: 'g5' }));
  const next = tool('read').renderCall({ path: 'c.py' }, theme, context({ toolCallId: 'g6' }));
  assert.match(screen(bash), /⏺ Bash\(ls\)/); assert.match(screen(next), /⏺ Read\(c\.py\)/);
});

test('welcome box and footer follow the Claude Code / Codex layout', () => {
  const box = welcomeBox({ workspace_mode: 'project', cwd: '/repo/project' }, theme, 80);
  assert.match(box[0], /^╭─+╮$/); assert.match(box.at(-1), /^╰─+╯$/);
  assert(box.some(line => line.includes('✻ KernelBridge')) && box.some(line => line.includes('ctrl+o')));
  assert(box.every(line => visibleWidth(line) === visibleWidth(box[0])));
  assert(welcomeBox({ workspace_mode: 'project' }, theme, 20).every(line => visibleWidth(line) <= 20));
  let footer;
  const ui = { getContextUsage: () => ({ percent: 12.4, contextWindow: 100000 }),
    ui: { setHeader() {}, setFooter(factory) { footer = factory({}, theme, { getGitBranch: () => 'main' }); } } };
  installProductUI(ui, () => ({ workspace_mode: 'project', model: 'fixture', cwd: '/repo/project', inputs: {} }));
  const rows = footer.render(72);
  assert.match(rows[0], /● fixture · 等待输入 +上下文剩余 88%$/);
  assert.match(rows.at(-1), /\/repo\/project \(main\) +\/help$/);
  // A long path gives up room, but the branch and the hint stay whole.
  installProductUI(ui, () => ({ workspace_mode: 'project', model: 'fixture', inputs: {},
    cwd: '/repo/a-long-intermediate-directory/another-long-one/leaf-project' }));
  const narrow = footer.render(48).at(-1);
  assert.ok(narrow.includes('leaf-project (main)') && narrow.endsWith('/help'), narrow);
  assert.ok(visibleWidth(narrow) <= 48, narrow);
  assert(rows.every(line => visibleWidth(line) <= 72));
  assert.match(workingMessage({ subagents: [] }, Date.now(), '运行命令'), /^运行命令… \(0s · esc 中断\)$/);
  // Queued input is visible while a turn runs, and only when something is queued.
  const queued = { subagents: [], inputs: { a: { status: 'queued' }, b: { status: 'queued' }, c: { status: 'applied' } } };
  assert.match(workingMessage(queued, Date.now()), /^执行中… \(0s · 2 条排队 · esc 中断\)$/);
  assert.equal(queuedInputs({ inputs: { a: { status: 'applied' } } }), 0);
});

test('the footer keeps the directory name when the path is too long', () => {
  const deep = '/repo/a-very-long-intermediate-directory/another-long-one/leaf-project';
  assert.equal(elidePath(deep, 80), deep);
  const elided = elidePath(deep, 30);
  assert.ok(visibleWidth(elided) <= 30 && elided.endsWith('leaf-project') && elided.includes('…'), elided);
  assert.ok(visibleWidth(elidePath(deep, 8)) <= 8 && elidePath(deep, 8).includes('…'));
});

test('help is one two-column table in both modes, with the keys section', () => {
  for (const mode of ['project', 'isolated']) {
    const lines = commandHelp(mode).split('\n');
    assert.equal(lines[0], '命令');
    assert.ok(lines.includes('快捷键'));
    const rows = lines.filter(line => line.startsWith('  /'));
    assert.ok(rows.length >= 8, rows);
    const columns = new Set(rows.filter(line => / {2,}\S/.test(line.slice(2))).map(line => visibleWidth(line.slice(2).replace(/ {2,}\S[\s\S]*$/, ''))));
    assert.ok(columns.size >= 1);
    assert.ok(commandHelp(mode).includes('/agents') && commandHelp(mode).includes('/trace'));
    assert.ok(commandHelp(mode).includes('输入 / 查看全部可用命令'));
  }
  // A command wider than the column keeps its own line instead of pushing the column out.
  const wide = commandHelp('project').split('\n');
  assert.ok(wide.includes('  /goal pause | resume | clear'));
  // The whole block fits a 24-row, 48-column terminal without clipping.
  for (const mode of ['project', 'isolated']) {
    const lines = commandHelp(mode).split('\n');
    assert.ok(lines.length <= 16, `${mode}: ${lines.length} lines`);
    assert.ok(lines.every(line => visibleWidth(line) <= 48), lines);
  }
});

test('a structured-only tool call still names its arguments', () => {
  const call = directRenderers('evaluate_candidate')
    .renderCall({ contract: { suite: [] }, source: { files: {} } }, theme, context({ toolCallId: 'r1' }));
  assert.match(screen(call), /evaluate_candidate\(contract · source\)/);
  const scalars = directRenderers('pipeline_run').renderCall({ stage: 'B', record: {} }, theme, context({ toolCallId: 'r2' }));
  assert.match(screen(scalars), /pipeline_run\(B\)/);
});
