import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { withPreparedAgent } from '../source/apps/agent/src/harness/pi/sdk.mjs';
import { controlledContext } from '../source/apps/agent/src/loopx/context-control.ts';

// CI checks the real Pi lifecycle with a deterministic receipt. Local cross-
// language acceptance also supplies the unmodified real Pipeline.apply output.
const failure = {
  schema_version: 'kernelbridge.tensor-failure/v1',
  binding: { case_id: 'fixture', candidate_sha256: '1'.repeat(64), input_sha256: '2'.repeat(64) },
  comparison: { kind: 'equal', atol: 0, rtol: 0, require_finite: false },
  actual: { shape: [2], dtype: 'torch.float32', device: 'cpu' },
  expected: { shape: [2], dtype: 'torch.float32', device: 'cpu' },
  mismatch_count: 1,
  examples: [{ flat_index: 1, actual: '+inf', expected: 2 }],
  diagnostic_status: 'completed',
};
const evaluation = {
  checks: { build: true, semantics: false, full: false },
  correctness_failure: failure,
  source_sha256: '1'.repeat(64),
  artifacts: [{ path: 'attempts/attempt-0001/result.json', sha256: '3'.repeat(64), bytes: 1024 }],
};
const fixture = process.env.KERNELBRIDGE_TENSOR_FEEDBACK_FIXTURE
  ? JSON.parse(await readFile(process.env.KERNELBRIDGE_TENSOR_FEEDBACK_FIXTURE, 'utf8'))
  : {
      pipeline_reply: { status: 'completed', data: { run: 'original-F', summary: { evaluation } } },
      composite_reply: { status: 'completed', data: { feedback: evaluation, run: 'original-F' } },
    };

for (const [name, key] of [['pipeline_run', 'pipeline_reply'], ['pipeline_evaluate', 'composite_reply']]) {
  test(`${name} preserves original correctness failure in the next real Pi model request`, async () => {
    assert.ok(fixture[key] && typeof fixture[key] === 'object', `Missing ${key}`);
    assert.match(JSON.stringify(fixture[key]), /correctness_failure/);
    const original = { action_id: `original-${name}-receipt`, outcome: fixture[key] };
    const directory = await mkdtemp(join(tmpdir(), 'kb-tensor-feedback-'));
    const callId = `${name}-once`;
    let turns = 0;
    let toolCalls = 0;
    let completed = false;
    const client = { async call(method, params = {}) {
      if (method === 'hello') return {
        task_id: 'tensor-feedback', goal: 'Evaluate the local fixture and report its original failure.',
        execution_mode: 'sequential',
        tools: [{ name, description: 'Return the original evaluation receipt.',
          parameters: { type: 'object', properties: {}, additionalProperties: false }, credits: 0 }],
        native_harness: { schema_version: 'kernelcat.pi-harness/v1', adapter: 'pi@1.5.0',
          native_tools: [], mcp_server: 'kernelcat', timeout: 1 },
      };
      if (method === 'model_turn') {
        turns++;
        const messages = params.harness.messages;
        if (turns === 1) {
          assert.ok(!messages.some(message => message.role === 'toolResult'));
          return { proposal_id: 'first', text: '', parallel: false,
            calls: [{ id: callId, name, arguments: {} }] };
        }
        assert.equal(turns, 2);
        assert.equal(completed, true);
        const matching = messages.filter(message => message.role === 'toolResult' && message.toolCallId === callId);
        assert.equal(matching.length, 1);
        const result = matching[0];
        assert.equal(result.toolName, name);
        assert.equal(result.isError, original.outcome.status !== 'completed');
        const resultIndex = messages.indexOf(result);
        assert.ok(messages.slice(0, resultIndex).some(message => message.role === 'assistant'
          && Array.isArray(message.content) && message.content.some(block => block.type === 'toolCall'
            && block.id === callId && block.name === name)));
        assert.equal(result.content.length, 1);
        assert.equal(result.content[0].type, 'text');
        // This also checks all source identities, artifact descriptors and
        // verdict fields without teaching the adapter a fixture-specific shape.
        assert.deepEqual(JSON.parse(result.content[0].text), original);
        return { proposal_id: 'second', text: 'Original correctness failure received.', parallel: false, calls: [] };
      }
      if (method === 'tool') {
        toolCalls++;
        assert.equal(toolCalls, 1);
        assert.deepEqual(params, { id: callId, name, arguments: {} });
        completed = true;
        return structuredClone(original);
      }
      if (method === 'end_turn') return { stop: params.proposal_id === 'second', result: null };
      if (method === 'complete') return { status: 'finished' };
      throw new Error(`Unexpected RPC ${method}`);
    } };
    try {
      assert.equal((await withPreparedAgent(run => run(controlledContext(client, directory)))).status, 'finished');
      assert.equal(turns, 2);
      assert.equal(toolCalls, 1);
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });
}
