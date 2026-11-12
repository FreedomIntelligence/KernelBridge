// Width properties of host-rendered UI: random CJK, emoji, ASCII and long paths
// at random terminal widths. Every row must fit the width and no wide character
// or surrogate pair may be cut (Anthropic used the same kind of randomized check
// when rewriting Claude Code's renderer). Seeded, so a failure reproduces.
import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { initTheme } from '../../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js';
import { visibleWidth } from '../../source/apps/agent/node_modules/@earendil-works/pi-tui/dist/index.js';
import { elidePath, welcomeBox, installProductUI, workingMessage, fitFeedback, editorPlaceholder } from '../../source/apps/agent/src/harness/pi/product-ui.mjs';
import { commandHelp } from '../../source/apps/agent/src/harness/pi/command-policy.mjs';
import { directRenderers } from '../../source/apps/agent/src/harness/pi/tool-render.mjs';
import { agentsPanel } from '../../source/apps/agent/src/harness/pi/agents-panel.mjs';

initTheme();
const theme = { fg: (_color, value) => value, bold: value => value, bg: (_color, value) => value };
const SAMPLES = 250;

function random(seed) {
  return () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
}
const PIECES = ['fix', 'parser', 'solution.py', 'kernel', '测试', '修复解析器边界条件', '中文路径', '（全角）', '🚀', '✔', 'a'.repeat(30), ' ', '-', '_'];
function generator(seed) {
  const next = random(seed);
  const pick = list => list[Math.floor(next() * list.length)];
  const words = (min, max) => Array.from({ length: min + Math.floor(next() * (max - min + 1)) }, () => pick(PIECES)).join(next() < .5 ? ' ' : '');
  const path = () => '/' + Array.from({ length: 1 + Math.floor(next() * 7) }, () => words(1, 3).replace(/\s+/g, '')).join('/');
  return { next, pick, words, path, width: () => 16 + Math.floor(next() * 125) };
}

const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;
function fits(rows, width, label) {
  for (const row of rows) {
    assert.ok(visibleWidth(row) <= width, `${label} at ${width}: ${visibleWidth(row)} > ${width}: ${JSON.stringify(row)}`);
    assert.ok(!LONE_SURROGATE.test(row), `${label} at ${width}: split surrogate in ${JSON.stringify(row)}`);
  }
}

test('paths, welcome box, placeholder and help fit any width', () => {
  const g = generator(1);
  for (let i = 0; i < SAMPLES; i++) {
    const width = g.width();
    const path = g.path();
    fits([elidePath(path, width)], width, 'elidePath');
    const state = { workspace_mode: g.next() < .5 ? 'project' : 'task', cwd: path, model: g.words(1, 3) };
    fits(welcomeBox(state, theme, width), width, 'welcomeBox');
    fits([editorPlaceholder(state.workspace_mode, width - 2)], width - 2, 'placeholder');
    const help = fitFeedback(commandHelp(state.workspace_mode), width);
    fits(Array.isArray(help) ? help : String(help).split('\n'), width, 'help');
  }
});

test('footer fits with any model, path, goal, plan, children and budget', () => {
  const g = generator(2);
  let footer;
  const ui = { ui: { setHeader() {}, setFooter(factory) { footer = factory({}, theme, { getGitBranch: () => g.words(1, 2) }); } } };
  let state;
  installProductUI(ui, () => state);
  for (let i = 0; i < SAMPLES; i++) {
    const width = g.width();
    const steps = Array.from({ length: 1 + Math.floor(g.next() * 5) }, (_, index) => ({ step: g.words(1, 4).slice(0, 80), status: index === 0 ? 'in_progress' : g.pick(['pending', 'completed']) }));
    state = { workspace_mode: g.next() < .7 ? 'project' : 'task', cwd: g.path(), model: g.words(1, 4), task_version: 1, trials: [],
      goal: g.next() < .5 ? { goal_id: 'g', status: g.pick(['active', 'paused', 'awaiting_user']), objective: g.words(2, 6) } : null,
      plan: g.next() < .5 ? { plan: steps } : null,
      subagents: g.next() < .4 ? [{ agent: 'kernel-scout', status: 'running', task: g.words(2, 8) }] : [],
      budget: g.next() < .3 ? { limit: 200, spent: 17, reserved: 3 } : {}, inputs: {} };
    fits(footer.render(width), width, 'footer');
  }
});

test('working row stays on one line at any width', () => {
  const g = generator(3);
  for (let i = 0; i < SAMPLES; i++) {
    const columns = g.width();
    const state = { subagents: g.next() < .5 ? [{ status: 'running' }, { status: 'running' }] : [],
      inputs: Object.fromEntries(Array.from({ length: Math.floor(g.next() * 4) }, (_, k) => ['i' + k, { status: 'queued' }])) };
    fits([workingMessage(state, Date.now() - Math.floor(g.next() * 4e6), g.words(1, 3), columns)], columns - 2, 'workingMessage');
  }
});

test('plan cells and agents panel fit any width', () => {
  const g = generator(4);
  const renderers = directRenderers('update_plan');
  const context = { cwd: '/repo', state: {}, isPartial: false, isError: false, expanded: false, executionStarted: true, toolCallId: 'kc', invalidate() {} };
  for (let i = 0; i < SAMPLES; i++) {
    const width = g.width();
    const plan = Array.from({ length: 1 + Math.floor(g.next() * 6) }, () => ({ step: g.words(1, 5).slice(0, 80), status: g.pick(['pending', 'completed', 'in_progress']) }));
    fits(renderers.renderCall({ plan }, theme, context).render(width), width, 'plan header');
    const result = { isError: false, details: { outcome: { status: 'completed', data: { plan, explanation: g.next() < .5 ? g.words(2, 10) : null } } } };
    fits(renderers.renderResult(result, { expanded: false }, theme, { ...context, args: { plan } }).render(width), width, 'plan cell');
    const children = Array.from({ length: Math.floor(g.next() * 4) }, (_, k) => ({ id: 'c' + k, agent: g.pick(['kernel-scout', 'kernel-reviewer']),
      status: g.pick(['running', 'completed', 'failed']), description: g.words(1, 4), task: g.words(3, 12), summary: g.words(2, 10), steps: 2, tool_calls: 3 }));
    const types = [{ name: 'kernel-scout', description: g.words(2, 6), tools: '只读', source: '内置' }];
    const panel = agentsPanel(children, types, () => {}, () => {}, () => 30);
    fits(panel.render(width), width, 'agents panel');
  }
});

test('deterministic mode freezes the working row clock and the spinner', () => {
  const script = `import('${new URL('../../source/apps/agent/src/harness/pi/product-ui.mjs', import.meta.url).href}').then(m => {
    const a = m.workingMessage({}, Date.now() - 5000, '执行中', 80), b = m.workingMessage({}, Date.now() - 3725000, '执行中', 80);
    process.stdout.write(JSON.stringify([a, b])); })`;
  const run = env => JSON.parse(execFileSync(process.execPath, ['-e', script], { env: { ...process.env, ...env } }).toString());
  const [frozenA, frozenB] = run({ KERNELBRIDGE_UI_DETERMINISTIC: '1' });
  assert.equal(frozenA, frozenB); assert.match(frozenA, /执行中… \(— · esc 中断\)/);
  const [liveA, liveB] = run({ KERNELBRIDGE_UI_DETERMINISTIC: '0' });
  assert.notEqual(liveA, liveB);
});
