// Real owner composition and replay projection, without provider or device calls.
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { wireValidator } from '../source/apps/agent/src/clients/wire-schemas.ts';
import { compileContext, encodedBytes } from '../source/apps/agent/src/context/compiler.ts';
import { validatedSnapshot } from '../source/apps/agent/src/context/json-snapshot.ts';
import { SCHEMA } from '../source/apps/agent/src/generated/harness.ts';
import { createContextControl } from '../source/apps/agent/src/loopx/context-control.ts';
import { controlledModels } from '../source/apps/agent/src/loopx/model-control.ts';

const staticInstructions = 'General coding instructions. 中文\n'.repeat(900);
const validateHarness = wireValidator(SCHEMA.$id, 'HarnessInput');
const text = value => [{ type: 'text', text: value }];
function source(spent = 0, contextBytes = 262144) {
  return {
    task: { id: 'general-task', goal: 'Inspect the declared source.', provenance: { uri: 'task:fixture' } },
    skills: [], policy: { context_bytes: contextBytes, permissions: ['read_file'], routes: ['primary', 'fallback'] },
    budget: { limit: null, spent, reserved: 0 }, history: [{ action: 'prior-context', status: 'completed' }],
    projection: { version: 'fixture/v1', history_omitted: 0, authority: 'journal',
      context_byte_limit: contextBytes, latest_action_id: spent ? `action-${spent}` : null },
  };
}
function harness(systemPrompt = staticInstructions) {
  return {
    schema_version: 'kernelcat.pi-harness/v1', adapter: 'pi@1.5.0', system_prompt: systemPrompt,
    messages: [
      { role: 'user', content: 'Read the two declared references.' },
      { role: 'assistant', content: [
        { type: 'thinking', thinking: 'Read both before deciding.' },
        { type: 'toolCall', id: 'call-a', name: 'read', arguments: { path: 'a.txt' } },
        { type: 'toolCall', id: 'call-b', name: 'read', arguments: { path: 'b.txt' } },
      ] },
      { role: 'toolResult', toolCallId: 'call-a', toolName: 'read', isError: false, content: text('a') },
      { role: 'toolResult', toolCallId: 'call-b', toolName: 'read', isError: false, content: text('b') },
    ],
    // Deliberately nonalphabetical: composition must preserve declarations as supplied.
    tools: [
      { name: 'tool_search', description: 'Search available tools.', parameters: { type: 'object' } },
      { name: 'read', description: 'Read a file.', parameters: { type: 'object' } },
    ],
  };
}
function freeze(value) {
  if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); }
  return value;
}
function protectedSource(value) {
  assert.equal(value.context_layout, 'system-suffix/v2');
  const start = '\n\nKERNELCAT_CONTEXT_V2\n', end = '\nEND_KERNELCAT_CONTEXT_V2';
  assert.equal(value.system_prompt.split(start).length, 2);
  assert.equal(value.system_prompt.split(end).length, 2);
  assert.ok(value.system_prompt.endsWith(end));
  return JSON.parse(value.system_prompt.split(start)[1].slice(0, -end.length));
}

test('new owner calls preserve the complete static prefix and original conversation across changing context', async () => {
  const contexts = [source(0), source(1)];
  const raw = freeze(harness()), original = structuredClone(raw), sent = [];
  let prepared = 0;
  const owner = controlledModels({ async call(method, params) {
    if (method === 'hello') return { native_harness: {} };
    if (method === 'prepare_turn') return { routes: ['primary'], context_source: contexts[prepared++] };
    if (method === 'model') { sent.push(params); return { text: 'Continue.', calls: [] }; }
    throw Error(method);
  } });
  await owner.call('hello');
  await owner.call('model_turn', { harness: raw });
  await owner.call('model_turn', { harness: raw });
  assert.equal(sent.length, 2);
  sent.forEach((request, index) => {
    assert.ok(request.harness.system_prompt.startsWith(staticInstructions + '\n\n'));
    assert.deepEqual(protectedSource(request.harness), compileContext(contexts[index], { harnessConversation: true }).context);
    assert.deepEqual(protectedSource(request.harness), request.context);
    assert.deepEqual(request.harness.messages, original.messages);
    assert.deepEqual(request.harness.tools, original.tools);
  });
  const [before, after] = sent.map(request => Buffer.from(request.harness.system_prompt));
  const firstDifference = before.findIndex((value, index) => value !== after[index]);
  assert.ok(firstDifference >= Buffer.byteLength(staticInstructions + '\n\nKERNELCAT_CONTEXT_V2\n'));
  assert.deepEqual(raw, original);
  assert.equal(Object.hasOwn(raw, 'context_layout'), false);
});

test('same-route retry and fallback use refreshed complete context without appending old blocks', async () => {
  const contexts = [source(0), source(1), source(2)].map(freeze);
  const raw = freeze(harness()), original = structuredClone(raw), sent = [], notices = [];
  const owner = controlledModels({ async call(method, params) {
    if (method === 'hello') return { native_harness: {} };
    if (method === 'prepare_turn') return { routes: ['primary', 'fallback'], context_source: contexts[0] };
    if (method !== 'model') throw Error(method);
    sent.push(params);
    if (sent.length === 1) return { failed: true, context_source: contexts[1],
      retry: { route: 'primary', attempt: 1, max_retries: 1, delay_ms: 0 } };
    if (sent.length === 2) return { failed: true, context_source: contexts[2] };
    return { text: 'Recovered.', calls: [] };
  } }, undefined, notice => notices.push(notice));
  await owner.call('hello');
  assert.equal((await owner.call('model_turn', { harness: raw })).text, 'Recovered.');
  assert.deepEqual(sent.map(request => request.route), ['primary', 'primary', 'fallback']);
  assert.equal(notices.length, 1);
  sent.forEach((request, index) => {
    const context = protectedSource(request.harness);
    assert.deepEqual(context, compileContext(contexts[index], { harnessConversation: true }).context);
    assert.equal(context.budget.spent, index);
    assert.equal(context.budget.limit, null);
    assert.equal(request.harness.system_prompt.split('\n\nKERNELCAT_CONTEXT_V2\n')[0], staticInstructions);
    assert.deepEqual(request.harness.messages, original.messages);
    assert.deepEqual(request.harness.tools, original.tools);
  });
  assert.deepEqual(raw, original);
});

test('a charged retry without fresh context and a lost acknowledgement never dispatch another request', async () => {
  for (const failure of ['missing-context', 'lost-acknowledgement']) {
    let dispatches = 0;
    const owner = controlledModels({ async call(method) {
      if (method === 'hello') return { native_harness: {} };
      if (method === 'prepare_turn') return { routes: ['primary', 'fallback'], context_source: source() };
      if (method !== 'model') throw Error(method);
      dispatches += 1;
      if (failure === 'lost-acknowledgement') throw Error('Original receipt acknowledgement lost');
      return { failed: true, retry: { route: 'primary', attempt: 1, max_retries: 1, delay_ms: 0 } };
    } });
    await owner.call('hello');
    await assert.rejects(owner.call('model_turn', { harness: harness() }),
      failure === 'missing-context' ? /refreshed budget context/ : /acknowledgement lost/);
    assert.equal(dispatches, 1);
  }
});

test('the optional layout preserves legacy JSON and refuses null or unknown values at the wire boundary', () => {
  const legacy = { ...harness('KERNELCAT_CONTEXT_V1\n{}\nEND_KERNELCAT_CONTEXT_V1\nOriginal instructions.') };
  const encoded = JSON.stringify(legacy);
  const restored = validatedSnapshot(JSON.parse(encoded), validateHarness, 'Legacy fixture');
  assert.equal(JSON.stringify(restored), encoded);
  assert.equal(Object.hasOwn(restored, 'context_layout'), false);
  for (const context_layout of [null, 'system-suffix/v1', 'system-suffix/v3', 2]) {
    assert.throws(() => validatedSnapshot({ ...legacy, context_layout }, validateHarness, 'Unknown fixture'));
  }
});

test('the real replay projection counts the whole V2 input before dispatch', async () => {
  const directory = await mkdtemp(join(process.env.KERNELBRIDGE_TEST_TMP ?? tmpdir(), 'kb-context-prefix-'));
  try {
    const control = createContextControl(directory);
    const raw = harness('Required system instructions.');
    raw.messages = [{ role: 'user', content: 'Inspect the declared source.' }];
    raw.system_prompt += 'x'.repeat(4096 - encodedBytes(raw) - 1);
    assert.equal(encodedBytes(raw), 4095);
    for (const limit of [4096, 8192]) {
      const context = source(0, limit), composed = [];
      let dispatches = 0;
      const owner = control.wrap(controlledModels({ async call(method) {
        if (method === 'hello') return { native_harness: {} };
        if (method === 'prepare_turn') return { routes: ['primary'], context_source: context };
        if (method === 'model') { dispatches += 1; return { text: 'Accepted within limit.', calls: [] }; }
        throw Error(method);
      } }, (input, accepted) => { composed.push(input); return control.project(input, accepted); }));
      await owner.call('context_boundary', { messages: raw.messages, metadata: [] });
      await owner.call('hello');
      if (limit === 4096) {
        await assert.rejects(owner.call('model_turn', { harness: raw }), /Protected model input exceeds context_bytes/);
        assert.equal(dispatches, 0);
        assert.ok(encodedBytes(composed[0]) > limit);
      } else {
        assert.equal((await owner.call('model_turn', { harness: raw })).text, 'Accepted within limit.');
        assert.equal(dispatches, 1);
        assert.ok(encodedBytes(composed[0]) < limit);
      }
      assert.equal(composed.length, 1);
      assert.deepEqual(protectedSource(composed[0]), compileContext(context, { harnessConversation: true }).context);
      assert.ok(encodedBytes(raw) < limit);
    }
  } finally { await rm(directory, { recursive: true, force: true }); }
});
