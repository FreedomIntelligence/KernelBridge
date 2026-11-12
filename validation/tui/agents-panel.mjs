import test from 'node:test';
import assert from 'node:assert/strict';
import { initTheme } from '../../source/apps/agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js';
import { visibleWidth } from '../../source/apps/agent/node_modules/@earendil-works/pi-tui/dist/index.js';
import { agentsPanel } from '../../source/apps/agent/src/harness/pi/agents-panel.mjs';

initTheme();
test('open panel and selected report refresh from authoritative child state at 48 columns', () => {
  let children = [{ id: 'one', agent: 'kernel-scout', description: '核对验证流程', task: 'read original receipts', status: 'running', steps: 1 }];
  let stopped;
  const panel = agentsPanel(() => children, [], result => { stopped = result; }, () => {}, () => 24);
  const render = () => {
    const rows = panel.render(48);
    assert(rows.every(line => visibleWidth(line) <= 48));
    assert(rows.length <= 24);
    return rows.join('\n');
  };
  assert.match(render(), /运行中/); assert.match(render(), /核对验证流程/);
  panel.handleInput('\r'); assert.match(render(), /尚无报告/);
  children = [{ ...children[0], status: 'completed', elapsed_ms: 65000, summary: '# 原始结果\n\n原始回执已核对。'.repeat(30) }];
  assert.match(render(), /完成/); assert.match(render(), /1m05s/); assert.match(render(), /原始回执已核对/);
  panel.handleInput('\x1b'); assert.match(render(), /本会话/);
  panel.handleInput('s'); assert.equal(stopped, undefined, 'completed runs cannot be stopped from a stale view');
  children = [{ ...children[0], status: 'running' }, { id: 'new', agent: 'kernel-scout', status: 'running', task: 'new work' }];
  panel.handleInput('s'); assert.deepEqual(stopped, { stop: 'one' }, 'new runs cannot move the selected stop target');
});
