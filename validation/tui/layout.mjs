// D5 and D12 of docs/architecture/tui-polish-plan.md: the empty-editor hint, and
// host-rendered blocks that fit the real terminal width (48 columns and narrower).
import test from 'node:test';
import assert from 'node:assert/strict';
import { installProductUI, placeholderEditor, showProductFeedback, statePresentation, workingMessage }
  from '../../source/apps/agent/src/harness/pi/product-ui.mjs';
import { commandHelp } from '../../source/apps/agent/src/harness/pi/command-policy.mjs';
import { initTheme } from '../../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js';
import { KeybindingsManager } from '../../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/core/keybindings.js';
import { getEditorTheme, theme as piTheme } from '../../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/modes/interactive/theme/theme.js';
import { visibleWidth } from '../../source/apps/agent/node_modules/@earendil-works/pi-tui/dist/index.js';

initTheme();
const plain = text => text.replace(/\x1b\[[0-9;]*m/g, '').replace(/\x1b_[^\x07\x1b]*(\x07|\x1b\\)/g, '');
const fits = (lines, width) => lines.every(line => visibleWidth(line) <= width);

function editor(mode) {
  const tui = { terminal: { rows: 24, columns: 48 }, requestRender() {} };
  return placeholderEditor(() => ({ workspace_mode: mode }), () => piTheme)(tui, getEditorTheme(), new KeybindingsManager());
}

function feedback(message, width, rows = 24) {
  const context = { ui: { setWidget(_key, factory) { context.widget = factory?.({ terminal: { rows } }, piTheme); } } };
  showProductFeedback(context, message);
  return context.widget.render(width).map(plain);
}

test('the empty editor shows a hint that the first keystroke clears', () => {
  const project = editor('project');
  const wide = project.render(48).map(plain);
  assert.ok(wide[1].includes('试试：修复失败的测试 · / 命令 · ! shell'), wide[1]);
  assert.equal(visibleWidth(project.render(48)[1]), 48, 'the hint row keeps the editor width');
  const narrow = project.render(28).map(plain);
  assert.ok(fits(project.render(28), 28) && !narrow[1].includes('…') && narrow[1].includes('输入任务'), narrow[1]);
  project.handleInput('x');
  assert.ok(!plain(project.render(48).join('\n')).includes('试试'), 'typing hides the hint');
  project.setText('');
  assert.ok(plain(project.render(48)[1]).includes('试试'), 'an empty editor shows it again');
  const task = editor('isolated').render(48).map(plain);
  assert.ok(task[1].includes('输入约束或问题 · /goal 目标内容 持续执行'), task[1]);
  assert.ok(fits(editor('isolated').render(20), 20));
});

test('/goal keeps each label with its value and elides the path at the real width', () => {
  const state = { workspace_mode: 'project', model: 'fixture', goal: { objective: '1', status: 'paused', on_detach: 'pause' },
    cwd: '/Users/someone/Desktop/KernelBridge/.validation/run-r1/workspace/91c26acccc0479b709a2', inputs: {} };
  for (const width of [48, 40, 32]) {
    const lines = feedback(statePresentation(state), width);
    assert.ok(fits(lines, width), lines.join('\n'));
    const directory = lines.find(line => line.startsWith('目录：'));
    assert.ok(directory?.endsWith('91c26acccc0479b709a2') && directory.includes('…'), `${width}: ${lines.join('\n')}`);
    assert.ok(lines.every(line => line.includes('：') || line.startsWith('⏺')), `no value wraps away from its label at ${width}`);
  }
});

test('/help stays a table at 48 columns and never starts a description at column 0 when narrower', () => {
  for (const mode of ['project', 'isolated']) {
    const help = commandHelp(mode);
    assert.deepEqual(feedback(help, 48), ['⏺ ' + help.split('\n')[0], ...help.split('\n').slice(1)], 'unchanged at 48 columns');
    for (const width of [44, 40, 32]) {
      const lines = feedback(help, width, 40);
      assert.ok(fits(lines, width), `${mode}@${width}\n${lines.join('\n')}`);
      const loose = lines.filter(line => !line.startsWith(' ') && !['⏺ 命令', '快捷键'].includes(line));
      assert.deepEqual(loose, [], `${mode}@${width}: text wrapped to column 0`);
      for (const item of ['ctrl+o 展开工具输出', '! 运行 shell', '输入 / 查看全部可用命令']) {
        assert.ok(lines.some(line => line.includes(item)), `${mode}@${width}: "${item}" was split`);
      }
    }
  }
  // At 40 columns the project table tightens its column instead of stacking.
  const tight = feedback(commandHelp('project'), 40);
  assert.ok(tight.includes('  /resume          恢复历史会话'), tight.join('\n'));
  assert.ok(feedback(commandHelp('project'), 40, 24).length <= 17, 'fits the height left in a 24-row terminal');
});

test('the footer keeps the directory name and the branch on narrow terminals', () => {
  let footer;
  const context = { getContextUsage: () => ({ percent: 91 }),
    ui: { setHeader() {}, setFooter(factory) { footer = factory({}, piTheme, { getGitBranch: () => 'main' }); } } };
  installProductUI(context, () => ({ workspace_mode: 'isolated', model: 'fixture', task_version: 2, trials: [{}],
    goal: { objective: '1', status: 'active', on_detach: 'pause' }, inputs: {},
    cwd: '/repo/a-long-intermediate-directory/another-long-one/leaf-project' }));
  const wide = footer.render(48).map(plain);
  assert.ok(wide.at(-1).includes('leaf-project (main)') && wide.at(-1).endsWith('退出暂停 · /help'), wide.at(-1));
  const narrow = footer.render(32).map(plain);
  assert.ok(narrow.at(-1).includes('leaf-project (main)') && narrow.at(-1).endsWith('/help'), narrow.at(-1));
  assert.ok(narrow[0].includes('剩余 9%'), narrow[0]);
  for (const width of [48, 32, 24, 16]) assert.ok(fits(footer.render(width), width), `footer@${width}`);
});

test('the working row drops details instead of wrapping', () => {
  const state = { subagents: [{ status: 'running' }], inputs: { a: { status: 'queued' }, b: { status: 'queued' } } };
  const started = Date.now() - 65000;
  assert.equal(workingMessage(state, started), '等待 1 个子代理完成… (1m05s · 2 条排队 · esc 中断)');
  for (const columns of [80, 48, 40, 30, 24, 12]) {
    const text = workingMessage(state, started, '执行中', columns);
    assert.ok(visibleWidth(text) + 4 <= columns || columns < 8, `${columns}: ${text}`);
    if (columns >= 24) assert.ok(text.startsWith('等待 1 个子代理完成…'), text);
  }
  assert.equal(workingMessage(state, started, '执行中', 48), '等待 1 个子代理完成… (1m05s · 2 条排队)');
});
