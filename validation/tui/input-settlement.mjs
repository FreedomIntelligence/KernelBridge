import test from 'node:test';
import assert from 'node:assert/strict';
import { createHostActions } from '../../source/apps/agent/src/harness/pi/host-actions.mjs';
import { loadInteractiveMode } from '../../source/apps/agent/src/harness/pi/interactive-patch.mjs';

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
