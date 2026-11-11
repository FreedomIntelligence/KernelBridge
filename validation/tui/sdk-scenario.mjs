import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { ApplicationSession } from '../../source/apps/agent/src/sdk/index.ts';

const options = JSON.parse(await readFile(process.argv[2], 'utf8'));
const session = await ApplicationSession.create(options);
let hostPid;
try {
  const before = await session.state();
  hostPid = before.host_pid;
  const deltas = [];
  await session.observe();
  session.subscribe(event => { if (event.type === 'model_delta') deltas.push(event); });
  const input = await session.prompt('中文 multi-line\nRead, edit and evaluate.', { inputId: 'scripted-input' });
  assert.equal(input.status, 'queued');
  assert.deepEqual(await session.prompt('中文 multi-line\nRead, edit and evaluate.', { inputId: 'scripted-input' }), input);
  await assert.rejects(session.prompt('different', { inputId: 'scripted-input' }));
  const result = await session.run();
  assert.equal(result.inputs['scripted-input'].status, 'completed');
  assert.equal(result.pending_effect_ids.length, 0);
  assert.equal(result.evidence.length, 1);
  assert.equal(result.evidence[0].evidence_tier, 'E1');
  assert.equal(result.budget.spent, 15);
  assert.equal(result.model_turns, 4);
  assert.ok(deltas.some(event => event.delta.delta));
  const repeated = await session.prompt('中文 multi-line\nRead, edit and evaluate.', { inputId: 'scripted-input' });
  assert.equal(repeated.status, 'completed');
  const identity = session.applicationSessionId;
  session.dispose();
  const attached = await ApplicationSession.attach(identity);
  const observed = await attached.state();
  assert.equal(observed.application_session_id, identity);
  assert.equal(observed.budget.spent, result.budget.spent);
  await writeFile(options.output + '/scenario.json', JSON.stringify({ result: observed, deltas: deltas.length }, null, 2));
  attached.dispose();
  process.stdout.write(JSON.stringify({ status: 'passed', application_session_id: identity, deltas: deltas.length, spent: result.budget.spent }) + '\n');
} finally {
  session.dispose();
  if (hostPid) { try { process.kill(hostPid, 'SIGTERM'); } catch {} }
}
