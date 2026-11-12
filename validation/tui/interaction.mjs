import test from 'node:test';
import assert from 'node:assert/strict';
import { PRODUCT_COMMANDS, parseGoalCommand } from '../../source/apps/agent/src/harness/pi/command-policy.mjs';
import { createHostActions } from '../../source/apps/agent/src/harness/pi/host-actions.mjs';
import { loadInteractiveMode } from '../../source/apps/agent/src/harness/pi/interactive-patch.mjs';
import { installProductUI, statePresentation } from '../../source/apps/agent/src/harness/pi/product-ui.mjs';
import { RunController } from '../../source/apps/agent/src/run/controller.mjs';
import { visibleWidth } from '../../source/apps/agent/node_modules/@earendil-works/pi-tui/dist/index.js';

test('Goal syntax follows the documented Codex lifecycle and preserves objective text', () => {
  assert.deepEqual(parseGoalCommand('/goal'), { operation: 'status' });
  for (const operation of ['pause', 'resume', 'clear']) assert.deepEqual(parseGoalCommand('/goal ' + operation), { operation });
  for (const objective of ['1', '修复中文输入\n保留精度', 'pause the benchmark after collecting evidence', 'set up the project']) {
    assert.deepEqual(parseGoalCommand('/goal ' + objective), { operation: 'set', objective });
  }
  assert.deepEqual(parseGoalCommand('/goal on-detach continue'), { operation: 'on-detach', value: 'continue' });
  assert.throws(() => parseGoalCommand('/goal on-detach invalid'));
});

test('the real Pi autocomplete includes Goal and excludes ungoverned plugin commands', async () => {
  const { InteractiveMode } = await loadInteractiveMode();
  const provider = InteractiveMode.prototype.createBaseAutocompleteProvider.call({
    options: { hostActions: { commands: () => PRODUCT_COMMANDS } }, sessionManager: { getCwd: () => process.cwd() },
  });
  const options = { signal: new AbortController().signal };
  const all = await provider.getSuggestions(['/'], 0, 1, options);
  assert.deepEqual(all.items.map(item => item.value).sort(), PRODUCT_COMMANDS.map(item => item.name).sort());
  const goals = await provider.getSuggestions(['/go'], 0, 3, options);
  assert.equal(goals.items[0].value, 'goal', 'Goal is the first Tab completion ahead of fuzzy matches');
  assert.equal(await provider.getSuggestions(['/subagents-load-profile'], 0, 23, options), null);
  const completed = provider.applyCompletion(['/go'], 0, 3, goals.items[0], goals.prefix);
  assert.equal(completed.lines[0].trim(), '/goal');
});

test('direct Goal objectives pass through durable control and remain readable', async () => {
  const state = { task_version: 1, trials: [], budget: { spent: 16, reserved: 0, limit: 200 }, pending_effect_ids: [] };
  const controls = [];
  const client = { subscribe: () => () => {}, state: async () => state,
    input: async (text, options) => ({ text, input_id: options.inputId, status: 'queued', channel: 'tui' }),
    call: async () => ({}),
    async control(operation, params) {
      controls.push({ operation, ...params });
      state.goal = { objective: params.objective, status: 'active', on_detach: 'pause' };
      state.task_version++;
    },
  };
  const actions = createHostActions(client, {});
  const result = await actions.submit('/goal 1', {});
  assert.equal(controls.length, 1);
  assert.equal(controls[0].operation, 'set');
  assert.equal(controls[0].objective, '1');
  // /goal answers with a live block: a function the feedback widget re-reads each second.
  assert.equal(typeof result.message, 'function');
  assert.match(result.message(), /目标：1/);
  assert.doesNotMatch(result.message(), /checkpoint|pi_session|\{"/);
  const policyText = 'Continue with the original receipts.\n{"decision_id":"private-id"}';
  state.inputs = { policy: { kind: 'policy', initiator: 'loopx', text: policyText } };
  await actions.submit('/goal', {});
  assert.equal(actions.presentInput(policyText), 'Goal 自动继续');
  state.inputs.user = { kind: 'prompt', initiator: 'user', text: policyText };
  assert.equal(actions.presentInput(policyText), policyText, 'explicit user messages remain visible');
  assert.equal((await actions.submit('/subagents-load-profile', {})).action, 'reject');
  actions.dispose();
});

test('product footer keeps budget and exit policy visible at narrow widths', () => {
  let footer;
  const state = { task_version: 2, trial_id: '01234567-1234-1234-1234-123456789abc', trials: [{}],
    budget: { spent: 16, reserved: 0, limit: 200 }, pending_effect_ids: [] };
  installProductUI({ ui: { setHeader() {}, setFooter(factory) { footer = factory(); } } }, () => state);
  for (const width of [40, 48, 80, 136]) {
    const lines = footer.render(width);
    assert.ok(lines.every(line => visibleWidth(line) <= width));
    assert.match(lines.join('\n'), /16\/200/);
    // Zero reserved and zero pending are noise: Claude Code and Codex show neither.
    assert.doesNotMatch(lines.join('\n'), /预留 0|待处理 0/);
    assert.match(lines.join('\n'), /退出暂停/);
    assert.doesNotMatch(lines.join('\n'), /01234567|2\.1M|controlled/);
  }
  state.pending_effect_ids = ['one', 'two']; state.budget.reserved = 3;
  assert.match(footer.render(80).join('\n'), /预留 3 · 待处理 2/);
  assert.match(statePresentation(state), /\/goal 目标内容/);
});

test('a checkpointed final answer settles without a synthetic stopped assistant turn', async () => {
  const calls = [];
  const rpc = { async call(method) { calls.push(method); return { stop: false, result: null }; } };
  const controller = new RunController(rpc, undefined, undefined, undefined, undefined, { persistent: true });
  controller.active = { proposal_id: 'model-1' };
  assert.deepEqual(await controller.finish({ stopReason: 'stop', content: [{ type: 'text', text: 'Done' }] }), { action: 'end' });
  assert.deepEqual(calls, ['end_turn']);
  assert.equal(controller.stopped, false, 'queued or later user input may still run');
  controller.active = { proposal_id: 'model-2' };
  assert.deepEqual(await controller.finish({ stopReason: 'toolUse', content: [{ type: 'toolCall' }] }), { action: 'continue' });
  const finite = new RunController(rpc);
  finite.active = { proposal_id: 'legacy-1' };
  assert.deepEqual(await finite.finish({ stopReason: 'stop', content: [{ type: 'text', text: 'Done' }] }), { action: 'continue' },
    'the finite compatibility executor still obtains its authoritative stop result');
});
