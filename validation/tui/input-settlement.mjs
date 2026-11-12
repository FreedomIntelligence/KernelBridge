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

/** Keep enqueue before Pi's user entry, as in the real native input hook. */
async function admissionFixture(t, enqueue, { workspaceMode = 'project', toolNames = [], availableTools = toolNames } = {}) {
  const hooks = new Map(), entries = [], calls = [], accepted = [], notifications = [];
  const state = { cursor: 1, workspace_mode: workspaceMode, subagents: [], trials: [], inputs: {} };
  let stateFailure, nextDecision, observe;
  let activeTools = [...toolNames];
  const client = {
    subscribe(listener) { observe = listener; return () => {}; }, async observe() {},
    async state() {
      calls.push({ method: 'state' });
      if (stateFailure) throw stateFailure;
      return structuredClone(state);
    },
    async input(text, options) {
      const receipt = { input_id: options.inputId, text, kind: options.kind, channel: options.channel, status: 'queued' };
      state.inputs[receipt.input_id] = receipt;
      accepted.push(receipt);
      return structuredClone(receipt);
    },
    async call(method, params = {}) {
      calls.push({ method, ...params });
      if (method === 'next') return nextDecision ?? { receipt: Object.values(state.inputs).find(item => item.status === 'queued') };
      return {};
    },
  };
  const effects = { bind() {}, async call(method, params) {
    calls.push({ method, ...params });
    return enqueue({ method, params, state, calls });
  } };
  const manager = { getBranch: () => entries, getLeafId: () => undefined, getSessionId: () => 'pi-admission' };
  const context = { sessionManager: manager, ui: { setStatus() {}, notify(message, kind) { notifications.push({ message, kind }); } } };
  const governed = { restored: true, manifest: {}, controller: {}, hello: {
    tools: availableTools.map(name => ({ name })),
  }, session: {
    sessionManager: manager, isStreaming: false, isCompacting: false,
    getActiveToolNames: () => [...activeTools], setActiveToolsByName(tools) { activeTools = [...tools]; },
  }, async prompt(text) { return hooks.get('input')({ text }); } };
  const actions = createHostActions(client, effects);
  actions.extension({ on(name, handler) { hooks.set(name, handler); }, appendEntry(customType, data) {
    entries.push({ type: 'custom', customType, data });
  } });
  t.after(() => actions.dispose());
  await actions.bind(governed);
  await hooks.get('session_start')({}, context);
  calls.length = 0;
  return { actions, state, calls, accepted, entries, notifications, context, hooks,
    observe: event => observe(event), activeTools: () => [...activeTools],
    setStateFailure(error) { stateFailure = error; },
    setNextDecision(decision) { nextDecision = decision; },
    async submit(text = 'work') {
      const submitted = await actions.submit(text, { streaming: false, compacting: false });
      return submitted.action === 'continue' ? hooks.get('input')({ text: submitted.text }) : submitted;
    } };
}

test('explicit enqueue rejection never persists Pi input and releases local delivery state', async t => {
  const run = await admissionFixture(t, ({ params, state }) => {
    if (state.inputs[params.input_id].text === 'fixed') return { executor_input_id: params.input_id };
    Object.assign(state.inputs[params.input_id], { status: 'failed', reason: 'input_validation_failed', error_summary: 'text exceeds 65536 bytes' });
    return { status: 'rejected', input_id: params.input_id, reason: 'input_validation_failed',
      error_summary: 'text exceeds 65536 bytes', input_bytes: 65537, validation_errors: [{ type: 'string_too_long' }] };
  });
  assert.deepEqual(await run.submit(), { action: 'handled' });
  const original = run.accepted[0].input_id;
  assert.equal(run.state.inputs[original].status, 'failed');
  assert.equal(run.actions.inputs.deliveries.length, 0);
  assert.equal(run.actions.inputs.uncertain.size, 0);
  // Even a subsequent user message cannot accidentally consume a stale applying receipt.
  await run.hooks.get('message_end')({ message: { role: 'user' } }, run.context);
  assert.deepEqual(run.entries, []);
  assert.match(run.notifications[0].message, /input_validation_failed.*\ntext exceeds 65536 bytes/);
  assert.equal(run.notifications[0].kind, 'error');
  assert.equal(run.calls.filter(item => item.method === 'application_enqueue').length, 1);
  assert.equal(run.calls.some(item => item.method === 'transition'), false);
  assert.deepEqual(await run.submit('fixed'), { action: 'continue' });
  assert.equal(run.accepted[1].kind, 'prompt');
  await run.hooks.get('message_end')({ message: { role: 'user' } }, run.context);
  assert.deepEqual(run.entries.map(item => item.data.input_id), [run.accepted[1].input_id]);
});

test('lost enqueue ACK checks durable state before retrying only the original queued identity', async t => {
  let attempts = 0;
  const run = await admissionFixture(t, ({ params }) => {
    if (++attempts === 1) throw new Error('ACKNOWLEDGEMENT_TIMEOUT');
    return { executor_input_id: params.input_id };
  });
  assert.deepEqual(await run.submit(), { action: 'continue' });
  assert.equal(run.accepted.length, 1);
  assert.deepEqual(run.calls.map(item => item.method), ['application_enqueue', 'state', 'application_enqueue']);
  assert.deepEqual(run.calls.filter(item => item.method === 'application_enqueue').map(item => item.input_id),
    [run.accepted[0].input_id, run.accepted[0].input_id]);
  assert.equal(run.actions.inputs.uncertain.size, 0);
  await run.hooks.get('message_end')({ message: { role: 'user' } }, run.context);
  assert.equal(run.entries.length, 1);
});

test('enqueue uncertainty never replays an applied or terminal original receipt', async t => {
  for (const status of ['applied', 'completed', 'failed', 'interrupted']) await t.test(status, async t => {
    const run = await admissionFixture(t, ({ params, state }) => {
      state.inputs[params.input_id].status = status;
      throw new Error('ACK lost after durable transition');
    });
    assert.deepEqual(await run.submit(), { action: 'handled' });
    assert.equal(run.calls.filter(item => item.method === 'application_enqueue').length, 1);
    assert.equal(run.state.inputs[run.accepted[0].input_id].status, status);
    assert.equal(run.actions.inputs.uncertain.size, 0);
    await run.hooks.get('message_end')({ message: { role: 'user' } }, run.context);
    assert.deepEqual(run.entries, []);
    assert.equal(run.calls.some(item => item.method === 'transition'), false);
  });
});

test('repeated unknown enqueue remains retryable by original ID without blocking later admission', async t => {
  let fail = true;
  const run = await admissionFixture(t, ({ params }) => {
    if (fail) throw new Error('transport disconnected');
    return { executor_input_id: params.input_id };
  });
  assert.deepEqual(await run.submit(), { action: 'handled' });
  const original = run.accepted[0].input_id;
  assert.equal(run.state.inputs[original].status, 'queued');
  assert.equal(run.actions.inputs.consumed.has(original), false);
  assert.equal(run.actions.inputs.uncertain.has(original), true);
  await run.hooks.get('message_end')({ message: { role: 'user' } }, run.context);
  assert.deepEqual(run.entries, []);
  // A headless attempt yields on retained uncertainty, instead of spinning next/enqueue.
  const before = run.calls.length;
  await run.actions.headless();
  assert.ok(run.calls.length - before < 10);
  assert.equal(run.calls.some(item => item.method === 'next'), true);
  fail = false;
  assert.deepEqual(await run.submit(), { action: 'continue' });
  assert.equal(run.accepted.length, 1);
  assert.deepEqual(new Set(run.calls.filter(item => item.method === 'application_enqueue').map(item => item.input_id)), new Set([original]));
  assert.equal(run.actions.inputs.uncertain.size, 0);
  await run.hooks.get('message_end')({ message: { role: 'user' } }, run.context);
  assert.deepEqual(run.entries.map(item => item.data.input_id), [original]);
});

test('failed status lookup retains the original input and cannot create a replacement on resubmit', async t => {
  let run;
  run = await admissionFixture(t, () => {
    run.setStateFailure(new Error('status unavailable'));
    throw new Error('ACK lost');
  });
  assert.deepEqual(await run.submit(), { action: 'handled' });
  const original = run.accepted[0].input_id;
  await assert.rejects(run.submit(), /status unavailable/);
  assert.equal(run.accepted.length, 1);
  assert.equal(run.actions.inputs.uncertain.has(original), true);
  assert.equal(run.calls.filter(item => item.method === 'application_enqueue').length, 1);
  assert.equal(run.calls.some(item => item.method === 'transition'), false);
  run.setStateFailure(undefined);
  run.state.inputs[original].status = 'completed';
  assert.equal((await run.submit()).action, 'handled');
  assert.equal(run.accepted.length, 1);
  assert.equal(run.actions.inputs.uncertain.size, 0);
});

test('invalid automatic continuation displays its returned summary without creating or replaying an input', async t => {
  const run = await admissionFixture(t, () => assert.fail('rejected continuation cannot enter the executor'));
  run.state.goal = { status: 'awaiting_user' };
  run.setNextDecision({ action: 'wait', reason: 'continuation_input_invalid', error_summary: 'text exceeds 65536 bytes' });
  await run.actions.headless();
  assert.deepEqual(run.notifications, [{ message: 'Goal 自动继续输入被拒绝：text exceeds 65536 bytes', kind: 'error' }]);
  assert.equal(run.accepted.length, 0);
  assert.deepEqual(run.entries, []);
  // Later awaiting-user polls carry no new rejection; do not replay historical diagnostics.
  run.setNextDecision({ action: 'wait', reason: 'awaiting_user' });
  await run.actions.headless();
  assert.equal(run.notifications.length, 1);
});

test('waiting notices show original read entries without executing or settling them and do not repeat', async t => {
  const run = await admissionFixture(t, () => assert.fail('a waiting notice cannot execute a tool'));
  run.state.goal = { goal_id: 'original-goal', status: 'awaiting_user' };
  run.state.pending_effect_ids = ['original-unknown-effect'];
  run.state.calls = { original: { status: 'pending' } };
  const references = [
    { tool: 'read_receipt', arguments: { action_id: 'original-action' } },
    { tool: 'pipeline_read', arguments: { run: 'original-run', path: 'driver/中文 trace.log', offset: 4096 } },
    { tool: 'read_receipt', arguments: { action_id: 'original-compile' } },
    { tool: 'pipeline_read', arguments: { run: 'original-run', path: 'driver/run-log.json' } },
    { tool: 'read_receipt', arguments: { action_id: 'fifth-not-shown' } },
  ];
  const decision = { action: 'wait', reason: 'awaiting_original_receipt', summary: '原作业状态未知；等待原回执。', receipt_refs: references };
  const originalState = structuredClone(run.state);
  run.setNextDecision(decision);
  await run.actions.headless();
  assert.deepEqual(run.notifications, [{ kind: 'info', message: [
    'Goal 等待原任务回执。', decision.summary, '原回执读取入口：',
    ...references.slice(0, 4).map(reference => JSON.stringify(reference)),
  ].join('\n') }]);
  assert.deepEqual(run.state, originalState);
  assert.deepEqual(decision.receipt_refs, references);
  run.observe({ type: 'state', state: structuredClone(run.state) });
  await run.actions.headless();
  run.setNextDecision({ action: 'wait', reason: 'goal_inactive' });
  await run.actions.headless();
  assert.equal(run.notifications.length, 1);
  assert.equal(run.accepted.length, 0);
  assert.deepEqual(run.entries, []);
  assert.ok(run.calls.every(call => ['state', 'next'].includes(call.method)));
});

test('no-progress summary fallback is shown once per explicit resume boundary without invented receipt paths', async t => {
  const run = await admissionFixture(t, () => assert.fail('a no-progress wait cannot execute a tool'));
  run.state.goal = { goal_id: 'same-goal', status: 'awaiting_user' };
  run.setNextDecision({ action: 'wait', reason: 'no_executable_progress', error_summary: '同一证据没有变化。' });
  await run.actions.headless();
  await run.actions.headless();
  assert.deepEqual(run.notifications, [{ kind: 'info', message: 'Goal 暂无可执行进展，等待输入。\n同一证据没有变化。' }]);
  run.observe({ type: 'control', operation: 'resume' });
  await run.actions.headless();
  assert.equal(run.notifications.length, 2);
  assert.equal(run.notifications[0].message, run.notifications[1].message);
  assert.equal(run.accepted.length, 0);
  assert.ok(run.calls.every(call => ['state', 'next'].includes(call.method)));
});

test('the existing tool-free reason still displays one wait notice without admitting another input', async t => {
  const run = await admissionFixture(t, () => assert.fail('a tool-free wait cannot execute a tool'));
  run.state.goal = { goal_id: 'tool-free-goal', status: 'awaiting_user' };
  run.setNextDecision({ action: 'wait', reason: 'no_tool_progress', summary: 'Automatic continuation ended without a work-tool call.' });
  await run.actions.headless();
  await run.actions.headless();
  assert.deepEqual(run.notifications, [{ kind: 'info', message: 'Goal 暂无可执行进展，等待输入。\nAutomatic continuation ended without a work-tool call.' }]);
  assert.equal(run.accepted.length, 0);
  assert.deepEqual(run.entries, []);
  assert.ok(run.calls.every(call => ['state', 'next'].includes(call.method)));
});

test('isolated Goal exposes its registered blocked-only updater while retaining read and pipeline tools', async t => {
  const base = ['read', 'get_goal', 'pipeline_evaluate'];
  const run = await admissionFixture(t, () => assert.fail('schema synchronization cannot execute a tool'), {
    workspaceMode: 'isolated', toolNames: [...base, 'update_goal'],
  });
  assert.deepEqual(run.activeTools(), base);
  for (const status of ['active', 'awaiting_user', 'paused', 'completed', 'cleared']) {
    run.state.goal = { goal_id: 'isolated-goal', status };
    run.observe({ type: 'state', state: structuredClone(run.state) });
    assert.deepEqual(run.activeTools(), ['active', 'awaiting_user'].includes(status) ? [...base, 'update_goal'] : base);
    assert.equal(run.activeTools().includes('finish_goal'), false);
    assert.equal(run.activeTools().includes('update_plan'), false);
  }
  const unavailable = await admissionFixture(t, () => assert.fail('unavailable updater cannot run'), {
    workspaceMode: 'isolated', toolNames: base, availableTools: base,
  });
  unavailable.state.goal = { goal_id: 'without-updater', status: 'active' };
  unavailable.observe({ type: 'state', state: structuredClone(unavailable.state) });
  assert.deepEqual(unavailable.activeTools(), base);
});

test('project Goal retains completion and planning tools across active and awaiting states', async t => {
  const base = ['read', 'get_goal'];
  const managed = ['finish_goal', 'update_goal', 'update_plan'];
  const run = await admissionFixture(t, () => assert.fail('schema synchronization cannot execute a tool'), {
    toolNames: [...base, ...managed],
  });
  assert.deepEqual(run.activeTools(), base);
  for (const status of ['active', 'awaiting_user', 'paused', 'completed', 'cleared', 'active']) {
    run.state.goal = { goal_id: 'project-goal', status };
    run.observe({ type: 'state', state: structuredClone(run.state) });
    assert.deepEqual(run.activeTools(), ['active', 'awaiting_user'].includes(status) ? [...base, ...managed] : base);
  }
});
