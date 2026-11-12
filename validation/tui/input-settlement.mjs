import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHostActions } from '../../source/apps/agent/src/harness/pi/host-actions.mjs';
import { loadInteractiveMode } from '../../source/apps/agent/src/harness/pi/interactive-patch.mjs';

test('startup control receipts do not suppress the isolated initial task or replay it', async () => {
  const state = { workspace_mode: 'isolated', inputs: {
    detach: { input_id: 'detach', kind: 'control', status: 'completed' },
  } };
  const accepted = [];
  const client = { subscribe: () => () => {}, async observe() {},
    async state() { return structuredClone(state); }, async call() { return {}; },
    async input(text, options) {
      const receipt = { input_id: options.inputId, text, kind: options.kind, status: 'queued' };
      state.inputs[receipt.input_id] = receipt; accepted.push(receipt); return receipt;
    },
  };
  const actions = createHostActions(client, {});
  await actions.bind({ restored: false, hello: { goal: 'the declared task only' }, manifest: {},
    session: { sessionManager: { getBranch: () => [] } } });
  assert.equal(await actions.initial(), 'the declared task only');
  assert.equal(await actions.initial(), undefined);
  assert.equal(accepted.length, 1);
  assert.equal(actions.inputs.consume('the declared task only').input_id, accepted[0].input_id);
  actions.dispose();
});

test('input after Pi stops streaming queues through the host settlement boundary once', async () => {
  const { InteractiveMode } = await loadInteractiveMode();
  let observe;
  const accepted = [];
  const state = { workspace_mode: 'project', subagents: [], inputs: {
    previous: { kind: 'prompt', status: 'applied' },
  } };
  const client = {
    subscribe(listener) { observe = listener; return () => {}; },
    async input(text, options) {
      if (options.kind === 'prompt' && state.inputs.previous.status === 'applied') {
        throw new Error('session is busy; submit explicitly as steer or follow_up');
      }
      const receipt = { text, input_id: options.inputId, kind: options.kind, status: 'queued' };
      accepted.push(receipt);
      return receipt;
    },
  };
  const actions = createHostActions(client, {});
  observe({ type: 'state', state });
  const editor = { setText() {}, addToHistory() {} };
  const mode = {
    options: { hostActions: actions },
    ui: {}, editor, defaultEditor: editor,
    // Pi clears this before awaiting the host's agent_settled hook.
    session: { isStreaming: false, isCompacting: false },
    pendingUserInputs: [], flushPendingBashComponents() {},
  };
  InteractiveMode.prototype.setupEditorSubmitHandler.call(mode);
  await editor.onSubmit('AFTER_CANCEL');
  assert.equal(accepted.length, 1);
  assert.equal(accepted[0].kind, 'follow_up');
  assert.deepEqual(mode.pendingUserInputs, ['AFTER_CANCEL']);
  state.inputs.previous.status = 'interrupted';
  const text = await InteractiveMode.prototype.getUserInput.call(mode);
  assert.equal(text, 'AFTER_CANCEL');
  assert.equal(actions.inputs.consume(text).input_id, accepted[0].input_id);
  assert.equal(mode.pendingUserInputs.length, 0);
  assert.throws(() => actions.inputs.consume(text), /no durable host receipt/);
  // Once settled, ordinary input keeps its normal prompt semantics.
  await editor.onSubmit('NEXT');
  assert.equal(accepted[1].kind, 'prompt');
  actions.dispose();
});

test('lost observer recovers retained notifications over control without abort or resubmission', async () => {
  let observe;
  const state = { cursor: 12, workspace_mode: 'project', subagents: [], inputs: {
    notification: { input_id: 'original-notification', initiator: 'agent', kind: 'follow_up',
      channel: 'policy', status: 'queued', text: '[子代理结果] retained result' },
  } };
  const control = { subscribe: () => () => {}, async state() { return structuredClone(state); },
    async call() { return {}; }, async input() { assert.fail('must not submit a replacement notification'); } };
  const observer = { subscribe(listener) { observe = listener; return () => {}; }, async observe() {} };
  const actions = createHostActions(control, {}, { observer });
  const delivered = [];
  const governed = { manifest: {}, controller: { fail() { assert.fail('observer loss cannot fail execution'); } },
    session: { isStreaming: false, isCompacting: false, getActiveToolNames: () => [], setActiveToolsByName() {},
      abort() { assert.fail('observer loss cannot abort execution'); } },
    async prompt(text) {
      delivered.push(actions.inputs.consume(text));
      state.inputs.notification.status = 'completed';
    },
  };
  await actions.bind(governed);
  observe({ type: 'disconnected', error: 'slow observer' });
  await new Promise(resolve => setImmediate(resolve));
  observe({ type: 'disconnected', error: 'same disconnected observer' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(delivered.length, 1);
  assert.equal(delivered[0].input_id, 'original-notification');
  assert.equal(delivered[0].text, state.inputs.notification.text);
  actions.dispose();
});

/** Exercise the production native input/settlement hooks, with a retained input. */
async function settlementFixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'kb-failure-'));
  const path = join(directory, 'session.jsonl');
  await writeFile(path, '');
  const records = [], hooks = new Map(), entries = [];
  let observe, unknown = false;
  const state = { cursor: 1, workspace_mode: 'project', subagents: [], trials: [],
    goal: { goal_id: 'original-goal', status: 'active' }, inputs: {
      original: { input_id: 'original', text: 'work', kind: 'prompt', status: 'queued' },
    } };
  const client = {
    subscribe(listener) { observe = listener; return () => {}; }, async observe() {},
    async state() { return structuredClone(state); },
    async call(method, params = {}) {
      records.push({ method, ...structuredClone(params) });
      if (method === 'transition') Object.assign(state.inputs[params.input_id], params);
      return {};
    },
    async control(operation) {
      records.push({ method: 'control', operation });
      state.goal.status = operation === 'interrupt' ? 'awaiting_user' : operation;
    },
  };
  const effects = { bind() {}, async call(method, params = {}) {
    records.push({ method, ...structuredClone(params) });
    if (method === 'interrupt_turn') {
      if (unknown) throw new Error('InDoubt: original effect remains unresolved');
      return { calls: [], needs_checkpoint: true, proposal_id: 'original-proposal' };
    }
    return {};
  } };
  const manager = { getBranch: () => entries, getLeafId: () => 'leaf',
    getSessionId: () => 'pi', getSessionFile: () => path };
  const context = { sessionManager: manager, ui: { setStatus() {}, setWidget() {}, notify() {} } };
  const governed = { manifest: {}, controller: { active: 'original-proposal', failure: null },
    checkpoint: async () => ({ digest: 'original-checkpoint' }),
    session: { sessionManager: manager, getActiveToolNames: () => [], setActiveToolsByName() {}, abort() {} } };
  const actions = createHostActions(client, effects);
  actions.extension({ on(name, handler) { hooks.set(name, handler); }, appendEntry(customType, data) {
    entries.push({ type: 'custom', customType, data });
  } });
  t.after(async () => { actions.dispose(); await rm(directory, { recursive: true, force: true }); });
  await actions.bind(governed);
  await hooks.get('session_start')({}, context);
  actions.inputs.enqueue(state.inputs.original);
  assert.deepEqual(await hooks.get('input')({ text: 'work' }), { action: 'continue' });
  await hooks.get('message_end')({ message: { role: 'user' } }, context);
  entries.push({ type: 'message', id: 'user-entry', message: { role: 'user', content: 'work' } });
  await actions.beforeModel();
  hooks.get('agent_start')();
  records.length = 0;
  return { actions, state, records, governed, observe, hooks, setUnknown: () => { unknown = true; },
    settle: event => hooks.get('agent_settled')(event) };
}

test('internal abort records a bounded failure before settling and never invents a user interrupt', async t => {
  const run = await settlementFixture(t);
  run.governed.controller.failure = new Error('Invalid ReplayContextBoundary: ' + '界'.repeat(3000));
  await run.settle({ aborted: true });
  const failure = run.records[0];
  assert.equal(failure.method, 'configuration');
  assert.equal(failure.operation, 'harness_failure');
  assert.equal(failure.reason, 'harness_failure');
  assert.equal(failure.goal_id, 'original-goal');
  assert.deepEqual(failure.input_ids, ['original']);
  assert.match(failure.summary, /Invalid ReplayContextBoundary/);
  assert.ok(Buffer.byteLength(failure.summary) <= 8192);
  assert.equal(run.records.some(item => item.method === 'control'), false);
  assert.deepEqual(run.records.filter(item => ['interrupt_turn', 'end_turn'].includes(item.method))
    .map(item => item.method), ['interrupt_turn', 'end_turn']);
  assert.equal(run.state.inputs.original.status, 'failed');
  assert.equal(run.state.inputs.original.reason, 'harness_failure');
  // A poisoned worker must not flip the Goal active before a fresh connection.
  run.state.inputs.resume = { input_id: 'resume', status: 'queued', kind: 'control' };
  const response = await run.actions.submit('/goal resume', {}, run.state.inputs.resume);
  assert.match(response.message, /重新连接不会修复历史/);
  assert.equal(run.records.some(item => item.method === 'control'), false);
});

test('unknown effects retain the original applied input after the failure cause is recorded', async t => {
  const run = await settlementFixture(t);
  run.governed.controller.failure = new Error('Context boundary rejected');
  run.setUnknown();
  await assert.rejects(run.settle({ aborted: true }), /InDoubt/);
  assert.equal(run.records[0].operation, 'harness_failure');
  assert.equal(run.state.inputs.original.status, 'applied');
  assert.equal(run.records.some(item => ['end_turn', 'transition', 'control'].includes(item.method)), false);
});

test('explicit Esc and an external pause keep their control semantics despite cancellation errors', async t => {
  for (const operation of ['interrupt', 'pause']) await t.test(operation, async t => {
    const run = await settlementFixture(t);
    if (operation === 'interrupt') await run.actions.interrupt();
    else {
      run.state.goal.status = 'paused';
      run.observe({ type: 'control', operation: 'pause' });
    }
    run.governed.controller.failure = new Error('Cancellation from the explicit control');
    await run.settle({ aborted: true });
    assert.equal(run.records.some(item => item.operation === 'harness_failure'), false);
    assert.equal(run.state.goal.status, operation === 'interrupt' ? 'awaiting_user' : 'paused');
    assert.equal(run.state.inputs.original.status, 'interrupted');
    assert.equal(run.records.filter(item => item.method === 'control').length, operation === 'interrupt' ? 1 : 0);
  });
});

test('a failure is bound to its original Goal even if the state advances before settlement', async t => {
  const run = await settlementFixture(t);
  run.state.goal = { goal_id: 'replacement-goal', status: 'active' };
  run.observe({ type: 'state', state: structuredClone(run.state) });
  run.governed.controller.failure = new Error('Old context failed');
  await run.settle({ aborted: true });
  assert.equal(run.records[0].goal_id, 'original-goal');
});

test('an explicit execution limit is not relabelled as a harness failure', async t => {
  const run = await settlementFixture(t);
  run.governed.controller.stopResult = { status: 'step_limit' };
  run.governed.controller.failure = new Error('The known admission stop');
  await run.settle({ aborted: false });
  assert.equal(run.records.some(item => item.operation === 'harness_failure' || item.method === 'control'), false);
  assert.equal(run.state.inputs.original.status, 'failed');
});
