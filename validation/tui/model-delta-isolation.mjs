import assert from 'node:assert/strict';
import { AsyncLocalStorage } from 'node:async_hooks';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { createGovernedRuntime } from '../../source/apps/agent/src/harness/pi/runtime-factory.mjs';
import { WorkerLease } from '../../source/apps/agent/src/application/worker-lease.mjs';

// Exercise the production controlled stream; SDK display and transport are
// inert fixtures so arrival order is deterministic and no provider is called.
async function fixture() {
  const home = await mkdtemp(join(tmpdir(), 'kb-model-deltas-'));
  const previousAgentDir = process.env.PI_CODING_AGENT_DIR;
  const childContext = new AsyncLocalStorage();
  const listeners = new Set(), pending = new Map();
  let stream, tools = [];
  const session = {
    agent: { state: { get tools() { return tools; }, set tools(value) { tools = value; } } },
    systemPrompt: 'fixture', getAllTools: () => [], subscribe: () => () => {},
    bindExtensions: async () => {},
  };
  const sdk = {
    ModelRuntime: { create: async () => ({
      registerProvider(_name, provider) { stream = provider.streamSimple; }, getModel: () => ({}),
    }) },
    SettingsManager: { inMemory: () => ({}) },
    SessionManager: { inMemory: () => ({}) },
    DefaultResourceLoader: class {
      async reload() {} getExtensions() { return { errors: [] }; }
    },
    createAgentSession: async () => ({ session }),
    createAgentSessionRuntime: async (create, options) => {
      await create(options);
      return { session, setRebindSession() {}, async dispose() {} };
    },
  };
  const lease = new WorkerLease({
    async call(method, request) {
      assert.equal(method, 'effect');
      const identity = request.params?.permit ?? 'parent';
      return new Promise((resolve, reject) => pending.set(identity, {
        resolve, reject, generation: request.context.model_generation_id, callId: request.call_id,
      }));
    },
  }, {});
  const client = {
    async call(method, params) {
      if (method === 'hello') return { tools: [], goal: 'fixture' };
      assert(['model_turn', 'child_model_turn'].includes(method), method);
      return lease.call(method === 'model_turn' ? 'model' : 'child_model', params);
    },
  };
  const governed = await createGovernedRuntime(client, home, {
    sdk, childContext, InMemoryCredentialStore: class {}, Type: { Object: value => value },
    createPluginBridge: () => ({ factories: [] }),
    createAssistantMessageEventStream() {
      const rows = [];
      let finish;
      const done = new Promise(resolve => { finish = resolve; });
      return { rows, done, push(event) { rows.push(structuredClone(event)); }, end: finish };
    },
  }, {
    persistent: true,
    withModelGeneration: (generation, run) => lease.withModelGeneration(generation, run),
    subscribeModelDeltas(listener) { listeners.add(listener); return () => listeners.delete(listener); },
  });
  return {
    start(permit) {
      return childContext.run(permit ? { permit } : undefined, () => stream({ id: 'fixture', provider: 'kernelcat', api: 'openai-completions' }, {}, {}));
    },
    emit(identity, type, delta) {
      const generation = pending.get(identity)?.generation ?? identity;
      for (const listener of listeners) listener({ type, delta }, generation);
    },
    pending, listeners,
    async close() {
      await governed.dispose();
      if (previousAgentDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
      else process.env.PI_CODING_AGENT_DIR = previousAgentDir;
      await rm(home, { recursive: true, force: true });
    },
  };
}

test('concurrent controlled streams render only their own generation and release listeners', async () => {
  const run = await fixture();
  try {
    const parent = run.start(), first = run.start('child-a'), second = run.start('child-b');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(run.pending.size, 3, 'parent and independent child lanes must all be in flight');
    assert.equal(new Set([...run.pending.values()].map(row => row.generation)).size, 3);
    assert([...run.pending.values()].every(row => typeof row.generation === 'string' && row.callId !== row.generation));
    run.emit('stale-review-generation', 'text_delta', 'DO NOT DISPLAY');
    run.emit(undefined, 'text_delta', 'UNIDENTIFIED');
    run.emit('child-b', 'thinking_delta', 'B 思考');
    run.emit('child-a', 'text_delta', 'A 中文');
    run.emit('parent', 'text_delta', 'PARENT');
    run.emit('child-b', 'text_delta', 'B 结论');
    for (const [identity, { resolve }] of run.pending) resolve({ text: identity, calls: [] });
    await Promise.all([parent.done, first.done, second.done]);
    const deltas = value => value.rows.filter(row => row.type.endsWith('_delta')).map(row => row.delta);
    assert.deepEqual(deltas(parent), ['PARENT']);
    assert.deepEqual(deltas(first), ['A 中文']);
    assert.deepEqual(deltas(second), ['B 思考', 'B 结论']);
    assert.equal(run.listeners.size, 0);
  } finally { await run.close(); }
});

test('a failed stream releases only its listener while another child keeps streaming', async () => {
  const run = await fixture();
  try {
    const first = run.start('child-a'), second = run.start('child-b');
    await new Promise(resolve => setImmediate(resolve));
    // A known provider stop is not an uncertain transport failure and must not
    // poison the independent child lane.
    run.pending.get('child-a').resolve({ failed: true, reason: 'provider_failed' });
    await first.done;
    assert.equal(run.listeners.size, 1);
    run.emit('child-a', 'text_delta', 'LATE FAILED CHILD');
    run.emit('child-b', 'text_delta', 'B remains live');
    run.pending.get('child-b').resolve({ text: 'B remains live', calls: [] });
    await second.done;
    assert.equal(first.rows.at(-1).type, 'error');
    assert.deepEqual(second.rows.filter(row => row.type === 'text_delta').map(row => row.delta), ['B remains live']);
    assert.equal(run.listeners.size, 0);
  } finally { await run.close(); }
});

test('complete effect reply replaces a preview with dropped middle chunks', async () => {
  const run = await fixture();
  try {
    const parent = run.start(), child = run.start('child-a');
    await new Promise(resolve => setImmediate(resolve));
    const complete = '开始：已读取原始证据。\n中间内容只存在于最终回执，包含中文和详细结论。\n结束。';
    const childComplete = '子代理完整结论：原始文件逐条检查完成。';
    // Slow presentation consumers may lose any preview frames, including all
    // frames for a child. The reliable effect reply remains the final message.
    run.emit('parent', 'text_delta', '开始：');
    run.emit('parent', 'text_delta', '结束。');
    run.pending.get('parent').resolve({ text: complete, calls: [] });
    run.pending.get('child-a').resolve({ text: childComplete, calls: [] });
    const [final, childFinal] = await Promise.all([parent.done, child.done]);
    const text = message => message.content.filter(block => block.type === 'text').map(block => block.text).join('');
    assert.equal(parent.rows.filter(row => row.type === 'text_delta').map(row => row.delta).join(''), '开始：结束。');
    assert.equal(parent.rows.find(row => row.type === 'text_end').content, '开始：结束。');
    assert.equal(parent.rows.at(-1).type, 'done');
    assert.equal(text(parent.rows.at(-1).message), complete);
    assert.equal(text(final), complete);
    assert.equal(child.rows.filter(row => row.type === 'text_delta').length, 0);
    assert.equal(text(child.rows.at(-1).message), childComplete);
    assert.equal(text(childFinal), childComplete);
    assert.equal(run.listeners.size, 0);
  } finally { await run.close(); }
});

test('generation identity never reuses effect receipt IDs or leaks onto non-model calls', async () => {
  const requests = [];
  const lease = new WorkerLease({ async call(_method, request) { requests.push(request); return {}; } }, {});
  lease.bind({ input_id: 'original-input' });
  await lease.withModelGeneration('generation-original', async () => {
    await lease.call('prepare_turn');
    await lease.call('model', { route: 'first' });
    await lease.call('model', { route: 'fallback' });
    await lease.call('child_model', { permit: 'child', route: 'first' });
    await lease.call('application_compact');
  });
  await lease.call('model', { route: 'unscoped' });
  assert.equal(new Set(requests.map(row => row.call_id)).size, requests.length);
  assert.deepEqual(requests.map(row => row.context.model_generation_id), [undefined,
    'generation-original', 'generation-original', 'generation-original', 'generation-original', undefined]);
  assert(requests.every(row => row.context.input_id === 'original-input'));
});
