// update_plan presentation: the plan cell, its failure row, the footer line and
// the trace projection, at the widths the layout checks use.
import test from 'node:test';
import assert from 'node:assert/strict';
import { initTheme } from '../../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js';
import { visibleWidth } from '../../source/apps/agent/node_modules/@earendil-works/pi-tui/dist/index.js';
import { directRenderers, planRows } from '../../source/apps/agent/src/harness/pi/tool-render.mjs';
import { installProductUI, planFooter } from '../../source/apps/agent/src/harness/pi/product-ui.mjs';
import { projectTrace } from '../../source/apps/agent/src/trace/journal-trace.ts';

initTheme();
const plain = { fg: (_color, value) => value, bold: value => value, bg: (_color, value) => value };
const styled = { ...plain, strikethrough: value => `~${value}~`, italic: value => `_${value}_` };
const context = (extra = {}) => ({ cwd: '/repo', state: {}, isPartial: false, isError: false, expanded: false,
  executionStarted: true, toolCallId: 'kc_plan', invalidate() {}, ...extra });
const plan = [
  { step: '复现失败的测试', status: 'completed' },
  { step: '修复 parser 的边界条件并补充回归测试用例', status: 'in_progress' },
  { step: '跑全量测试', status: 'pending' },
];

test('plan cell: completed struck through, active step marked, pending dim, explanation first', () => {
  assert.deepEqual(planRows(plan, '拆成三步', styled), ['_拆成三步_', '✔ ~复现失败的测试~', '■ 修复 parser 的边界条件并补充回归测试用例', '□ 跑全量测试']);
  const renderers = directRenderers('update_plan');
  const header = renderers.renderCall({ plan }, plain, context()).render(80).join('\n');
  assert.match(header, /⏺ 更新计划\(1\/3 完成\)/);
  const result = { isError: false, details: { outcome: { status: 'completed', data: { plan, explanation: '拆成三步' } } } };
  for (const width of [80, 48, 32]) {
    const rows = renderers.renderResult(result, { expanded: false }, plain, context({ args: { plan } })).render(width);
    assert.ok(rows.every(row => visibleWidth(row) <= width), `${width}: ${rows.join('|')}`);
    const text = rows.join('\n');
    assert.match(text, /⎿ {2}拆成三步/); assert.match(text, /✔ 复现失败的测试/); assert.match(text, /□ 跑全量测试/);
  }
});

test('a refused plan shows the host reason, not a checklist', () => {
  const receipt = { isError: true, content: [{ type: 'text', text: '{"action_id":"x","outcome":{"status":"failed"}}' }],
    details: { outcome: { status: 'failed', data: { error: 'at most one plan step may be in_progress' } } } };
  const fromReceipt = directRenderers('update_plan').renderResult(receipt, { expanded: false }, plain, context()).render(60).join('\n');
  assert.match(fromReceipt, /失败：at most one plan step may be in_progress/);
  assert.doesNotMatch(fromReceipt, /action_id/, 'the receipt JSON is not shown');
  const result = { isError: true, content: [{ type: 'text', text: 'at most one plan step may be in_progress' }], details: {} };
  const text = directRenderers('update_plan').renderResult(result, { expanded: false }, plain, context()).render(60).join('\n');
  assert.match(text, /失败：at most one plan step may be in_progress/);
  assert.doesNotMatch(text, /□|■|✔/);
});

test('footer carries plan progress only while a Goal is active', () => {
  assert.equal(planFooter({ plan }), '计划 1/3 · 正在：修复 parser 的边界条件并补充回归测试用例');
  assert.equal(planFooter({ plan: plan.map(item => ({ ...item, status: 'completed' })) }), '计划 3/3 · 已全部完成');
  let footer;
  const ui = { ui: { setHeader() {}, setFooter(factory) { footer = factory({}, plain, { getGitBranch() {} }); } } };
  const state = { workspace_mode: 'project', model: 'fixture', goal: { status: 'active', goal_id: 'g' }, plan: { plan } };
  installProductUI(ui, () => state);
  for (const width of [80, 48, 32]) {
    const rows = footer.render(width);
    assert.ok(rows.some(row => row.startsWith('计划 1/3')), rows.join('|'));
    assert.ok(rows.every(row => visibleWidth(row) <= width), `${width}: ${rows.join('|')}`);
  }
  state.goal = null;
  assert.ok(!footer.render(80).some(row => row.startsWith('计划')), 'no plan line without a Goal');
});

test('trace shows a plan update as a titled checklist', () => {
  const S = 1_800_000_000_000_000_000; let seq = 0; const rows = [];
  const add = row => rows.push({ seq: seq++, ...row });
  add({ kind: 'task', task: { id: 't', goal: 'g' } });
  add({ kind: 'application/input_accepted', application_session_id: 'a', time_ns: S, data: { kind: 'prompt', initiator: 'user', text: 'go' } });
  add({ kind: 'admitted', action: { id: 't/step/0/model/primary', tool: '__propose__', task: 't', arguments: {}, parents: [] } });
  add({ kind: 'settled', action_id: 't/step/0/model/primary', elapsed_ms: 100, outcome: { status: 'completed', data: { content: [{ type: 'toolCall', id: 'k', name: 'update_plan' }], usage: { prompt_tokens: 100, completion_tokens: 10 } } } });
  add({ kind: 'admitted', action: { id: 't/step/0/model/primary/call/0', tool: 'update_plan', task: 't', arguments: { plan }, parents: ['t/step/0/model/primary'] } });
  add({ kind: 'settled', action_id: 't/step/0/model/primary/call/0', elapsed_ms: 1, outcome: { status: 'completed', data: { plan, explanation: '拆成三步' } } });
  add({ kind: 'application/call_result', application_session_id: 'a', time_ns: S + 2e8, data: { call_id: 'c', result: {} } });
  const step = projectTrace(rows, { now_ns: S + 1e9 }).steps.find(s => s.tool === 'update_plan');
  assert.equal(step.title, '更新计划 1/3');
  assert.equal(step.out, '拆成三步\n\n✔ 复现失败的测试\n■ 修复 parser 的边界条件并补充回归测试用例\n□ 跑全量测试');
});
