import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { ApplicationSession } from '../../source/apps/agent/src/sdk/index.ts';

const options = JSON.parse(await readFile(process.argv[2], 'utf8'));
const session = await ApplicationSession.create(options);
let pid;
try {
  pid = (await session.state()).host_pid;
  await session.prompt('Run the local fixture, retaining immutable reference and original receipts.');
  let state = await session.run();
  const original = state.checkpoint;
  const originalBytes = await readFile(original.session_file, 'utf8');
  const source = join(state.cwd, 'solution.py');
  assert.equal(await readFile(source, 'utf8'), 'x = 2\n');
  const historical = Object.values(state.checkpoints).find(item => item.source_sha256 !== original.source_sha256);
  assert.ok(historical, 'a historical source checkpoint must exist');
  const before = { spent: state.budget.spent, models: state.model_turns, trials: state.trials.length };
  await session.fork(historical.entry_id);
  state = await session.run();
  assert.notEqual(state.checkpoint.pi_session_id, original.pi_session_id);
  assert.equal(await readFile(source, 'utf8'), 'x = 1\n');
  assert.equal(state.budget.spent, before.spent);
  assert.equal(state.model_turns, before.models);
  assert.equal(state.trials.length, before.trials + 1);
  await session.command(`/resume ${original.session_file}`);
  state = await session.run();
  assert.equal(state.checkpoint.pi_session_id, original.pi_session_id);
  // Restart may use a writable staging copy of the immutable checkpoint;
  // session identity, history and source are the resume contract.
  assert.equal(await readFile(original.session_file, 'utf8'), originalBytes);
  assert.equal(await readFile(source, 'utf8'), 'x = 2\n');
  assert.equal(state.trials.length, before.trials + 1);
  assert.equal(state.model_turns, before.models);
  assert.equal(state.budget.spent, before.spent);
  const rejected = await session.command('/fork unknown-entry');
  await assert.rejects(session.run(), /Unknown fork entry/);
  state = await session.state();
  assert.equal(state.inputs[rejected.input_id].status, 'failed');
  assert.equal(await readFile(source, 'utf8'), 'x = 2\n');
  assert.equal(await readFile(join(state.cwd, 'reference/spec.txt'), 'utf8'), 'immutable');
  await writeFile(options.output + '/scenario.json', JSON.stringify({ status: 'passed',
    checks: ['historical_source_restored', 'original_session_resume', 'no_replay_or_extra_trial_on_resume',
      'shared_budget_retained', 'unknown_entry_rejected', 'immutable_reference_preserved'] }, null, 2));
  process.stdout.write('Source-bound fork and resume passed.\n');
} finally {
  session.dispose();
  if (pid) { try { process.kill(pid, 'SIGTERM'); } catch {} }
}
