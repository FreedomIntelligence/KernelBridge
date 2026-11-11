import test from 'node:test';
import assert from 'node:assert/strict';
import { installProductUI, renderReceiptResult, renderDelegateCall } from '../../source/apps/agent/src/harness/pi/product-ui.mjs';

const theme = { fg: (_color, value) => value };
test('child status remains visible in a narrow footer and partial result is explicit', () => {
  let footer;
  const context = { ui: { setHeader() {}, setFooter(factory) { footer = factory({}, theme, { getGitBranch() {} }); } } };
  const state = { workspace_mode: 'project', model: 'fixture', subagents: [
    { agent: 'kernel-scout', status: 'running', steps: 2, tool_calls: 1, task: '梳理开发流程'.repeat(30) },
  ] };
  installProductUI(context, () => state);
  const rows = footer.render(48);
  assert(rows.some(line => line.includes('子代理 kernel-scout')));
  assert(rows.some(line => line.includes('执行中')));
  const rendered = renderReceiptResult({ details: { outcome: { status: 'completed', data: {
    agent: 'kernel-reviewer', partial: true, completion_reason: 'step_limit', steps: 6, tool_calls: 5,
    summary: 'Known evidence; unresolved question remains.',
  } } } }, { expanded: false }, theme).render(80).join('\n');
  assert.match(rendered, /部分结果/);
  assert.match(rendered, /unresolved question/);
  const call = renderDelegateCall({ agent: 'kernel-scout', task: 'inspect\x1b[2Jcode' }, theme).render(80).join('\n');
  assert(!call.includes('\x1b'));
});
