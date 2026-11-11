import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { ApplicationSession } from '../../source/apps/agent/src/sdk/index.ts';

const options = JSON.parse(await readFile(process.argv[2], 'utf8'));
let session = await ApplicationSession.create(options);
let hostPid;
try {
  hostPid = (await session.state()).host_pid;
  await session.prompt('Start the local fixture; retain constraint 中文 forever.', { inputId: 'initial' });
  let state = await session.run();
  assert.equal(state.model_turns, 4);
  const originalSession = state.checkpoint.pi_session_id;
  // Twenty accepted user turns across three process lifetimes, one native Trial.
  for (let batch = 0; batch < 3; batch++) {
    for (let i = batch * 7; i < Math.min((batch + 1) * 7, 20); i++) {
      await session.followUp(`Continue local fixture turn ${i}; retain the original Chinese constraint.`, { inputId: `long-${i}` });
    }
    state = await session.run();
    assert.equal(state.checkpoint.pi_session_id, originalSession);
    const identity = session.applicationSessionId;
    session.dispose();
    session = await ApplicationSession.attach(identity);
  }
  assert.equal(state.model_turns, 24);
  assert.equal(state.trials.length, 1);
  assert.equal(state.budget.spent, 35);
  for (let i = 0; i < 20; i++) assert.equal(state.inputs[`long-${i}`].status, 'completed');
  await session.command('/compact', { inputId: 'compact' });
  await session.prompt('After compression, retain the original constraints.', { inputId: 'after-compact' });
  state = await session.run();
  assert.equal(state.inputs.compact.status, 'completed');
  const rows = (await readFile(options.output + '/state/events.jsonl', 'utf8')).trim().split('\n').map(JSON.parse);
  const compactions = rows.filter(row => row.kind === 'admitted' && row.action.tool === '__propose__' && row.action.id.includes('/compact/'));
  assert.ok(compactions.length > 0);
  assert.equal(state.model_turns, 25 + compactions.length);
  assert.equal(state.budget.spent, 36 + compactions.length);
  assert.equal(state.trials.length, 1);
  await session.fork();
  await session.prompt('Continue the new Trial from the source-bound fork.', { inputId: 'after-fork' });
  state = await session.run();
  assert.equal(state.trials.length, 2);
  assert.notEqual(state.checkpoint.pi_session_id, originalSession);
  assert.equal(state.pending_effect_ids.length, 0);
  assert.equal(state.inputs['after-fork'].status, 'completed');
  const sessionBytes = await readFile(state.checkpoint.session_file, 'utf8');
  assert.match(sessionBytes, /中文/);
  assert.match(sessionBytes, /kernelbridge-retained-facts/);
  await writeFile(options.output + '/scenario.json', JSON.stringify({ status: 'passed', turns: 20, state }, null, 2));
  process.stdout.write(JSON.stringify({ status: 'passed', turns: 20, model_turns: state.model_turns, spent: state.budget.spent, trials: state.trials.length }) + '\n');
} finally {
  session.dispose();
  if (hostPid) { try { process.kill(hostPid, 'SIGTERM'); } catch {} }
}
