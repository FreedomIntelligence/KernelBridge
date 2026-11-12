// Golden snapshots of host-rendered UI blocks at 32, 40, 48, 80 and 110 columns
// (Codex keeps insta snapshots of its TUI the same way). A changed block fails
// until the snapshot is regenerated on purpose:
//   UPDATE_SNAPSHOTS=1 node --test validation/tui/snapshots.mjs
// The blocks emit their final lines, so the text is what the terminal shows; how
// they compose into a full screen stays with the PTY scenarios. Clocks and the
// spinner are frozen with KERNELBRIDGE_UI_DETERMINISTIC.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';

process.env.KERNELBRIDGE_UI_DETERMINISTIC = '1';
const DIR = new URL('./snapshots/', import.meta.url);
const WIDTHS = [32, 40, 48, 80, 110];
const UPDATE = process.env.UPDATE_SNAPSHOTS === '1';
const { initTheme } = await import('../../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js');
const { welcomeBox, installProductUI, workingMessage, fitFeedback, editorPlaceholder, planFooter } = await import('../../source/apps/agent/src/harness/pi/product-ui.mjs');
const { commandHelp } = await import('../../source/apps/agent/src/harness/pi/command-policy.mjs');
const { directRenderers, withProductRenderers, explorationBoundary } = await import('../../source/apps/agent/src/harness/pi/tool-render.mjs');
const { agentsPanel } = await import('../../source/apps/agent/src/harness/pi/agents-panel.mjs');
initTheme();

const theme = { fg: (_color, value) => value, bold: value => value, bg: (_color, value) => value };
const context = (extra = {}) => ({ cwd: '/home/dev/projects/kernel-bridge', state: {}, isPartial: false, isError: false, expanded: false,
  executionStarted: true, toolCallId: 'kc_snap', invalidate() {}, ...extra });
const lines = value => Array.isArray(value) ? value : String(value).split('\n');
const plan = [{ step: '复现失败的测试', status: 'completed' }, { step: '修复 parser 的边界条件', status: 'in_progress' }, { step: '跑全量测试', status: 'pending' }];
const project = { workspace_mode: 'project', model: 'deepseek-v4', cwd: '/home/dev/projects/kernel-bridge', inputs: {}, subagents: [] };

function footerOf(state) {
  let footer;
  installProductUI({ ui: { setHeader() {}, setFooter(factory) { footer = factory({}, theme, { getGitBranch: () => 'main' }); } } }, () => state);
  return width => footer.render(width);
}

const BLOCKS = {
  'welcome-project': width => welcomeBox(project, theme, width),
  'welcome-task': width => welcomeBox({ ...project, workspace_mode: 'task' }, theme, width),
  'help-project': width => lines(fitFeedback(commandHelp('project'), width)),
  'help-task': width => lines(fitFeedback(commandHelp('task'), width)),
  'placeholder-project': width => [editorPlaceholder('project', width - 2)],
  'footer-idle': footerOf(project),
  'footer-goal-plan-children': footerOf({ ...project, goal: { goal_id: 'g', status: 'active', on_detach: 'pause' }, plan: { plan },
    subagents: [{ agent: 'kernel-scout', status: 'running', task: '梳理调用链' }], inputs: { a: { status: 'applied', kind: 'prompt' } } }),
  'footer-task-budget': footerOf({ ...project, workspace_mode: 'task', task_version: 2, trials: [{}], budget: { limit: 200, spent: 17, reserved: 0 } }),
  'working-row': width => [workingMessage({ subagents: [{ status: 'running' }], inputs: { q: { status: 'queued' } } }, 0, '执行中', width)],
  'plan-footer': () => [planFooter({ plan })],
  'plan-cell': width => {
    const renderers = directRenderers('update_plan');
    return [...renderers.renderCall({ plan }, theme, context()).render(width),
      ...renderers.renderResult({ isError: false, details: { outcome: { status: 'completed', data: { plan, explanation: '拆成三步' } } } },
        { expanded: false }, theme, context({ args: { plan } })).render(width)];
  },
  'tool-read-edit': width => {
    explorationBoundary();
    const tool = name => withProductRenderers({ name, execute() {} });
    const read = tool('read'); const edit = tool('edit');
    const rows = [...read.renderCall({ path: '/home/dev/projects/kernel-bridge/src/parser.py' }, theme, context({ toolCallId: 'r1', expanded: true })).render(width),
      ...read.renderResult({ content: [{ type: 'text', text: 'line 1\nline 2\nline 3' }] }, { expanded: false }, theme, context({ toolCallId: 'r1', expanded: true })).render(width)];
    explorationBoundary();
    rows.push(...edit.renderCall({ path: 'src/parser.py', edits: [{ oldText: 'return None', newText: 'return []' }] }, theme, context({ toolCallId: 'e1' })).render(width));
    return rows;
  },
  'agents-panel': width => agentsPanel([{ id: 'c1', agent: 'kernel-scout', status: 'completed', description: '梳理调用链', task: '找出 parse 的调用方', summary: 'main.py 调用了它', steps: 3, tool_calls: 4, tokens: 900, elapsed_ms: 15000 }],
    [{ name: 'kernel-scout', description: '查找文件、搜索代码、追踪实现', tools: '只读', source: '内置' }, { name: 'kernel-tester', description: '运行测试、构建和诊断', tools: '可运行命令', source: '内置' }],
    () => {}, () => {}, () => 30).render(width),
};

test('host-rendered blocks match their snapshots at every width', () => {
  mkdirSync(DIR, { recursive: true });
  const changed = [];
  for (const [name, render] of Object.entries(BLOCKS)) {
    for (const width of WIDTHS) {
      const text = render(width).map(line => line.replace(/\s+$/, '')).join('\n') + '\n';
      const file = new URL(`${name}@${width}.txt`, DIR);
      if (UPDATE) { writeFileSync(file, text); continue; }
      if (!existsSync(file)) { changed.push(`${name}@${width} (missing; run with UPDATE_SNAPSHOTS=1)`); continue; }
      if (readFileSync(file, 'utf8') !== text) changed.push(`${name}@${width}`);
    }
  }
  assert.deepEqual(changed, [], 'changed snapshots:\n' + changed.join('\n'));
});
