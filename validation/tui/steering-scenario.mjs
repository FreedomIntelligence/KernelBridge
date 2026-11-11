import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { ApplicationSession } from '../../source/apps/agent/src/sdk/index.ts';

const options = JSON.parse(await readFile(process.argv[2], 'utf8'));
const session = await ApplicationSession.create(options);
const hostPid = (await session.state()).host_pid;
const until = async predicate => {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) { if (await predicate()) return; await new Promise(r => setTimeout(r, 25)); }
  throw new Error('Steering scenario timed out');
};
try {
  await session.prompt('Initial admitted input.', { inputId: 'initial' });
  const running = session.run();
  await until(async () => (await session.state()).inputs.initial.status === 'applied');
  await assert.rejects(session.prompt('Ambiguous busy prompt'), /busy/);
  await session.steer('Steer exactly once: preserve 中文.', { inputId: 'steer' });
  await session.followUp('Follow up exactly once after the current run.', { inputId: 'follow' });
  let state = await running;
  for (const key of ['initial', 'steer', 'follow']) assert.equal(state.inputs[key].status, 'completed');
  assert.equal(state.evidence.length, 1);
  await session.prompt('Interrupt this generation.', { inputId: 'cancel' });
  const interrupted = session.run();
  await until(async () => (await session.state()).inputs.cancel.status === 'applied');
  await session.interrupt();
  state = await interrupted;
  assert.equal(state.inputs.cancel.status, 'interrupted');
  assert.equal(state.pending_effect_ids.length, 0);
  await session.prompt('Continue after the explicit interruption.', { inputId: 'resume' });
  state = await session.run();
  assert.equal(state.inputs.resume.status, 'completed');
  const lines = (await readFile(state.checkpoint.session_file, 'utf8')).trim().split('\n').map(JSON.parse);
  for (const text of ['Steer exactly once: preserve 中文.', 'Follow up exactly once after the current run.']) {
    assert.equal(lines.filter(row => row.type === 'message' && row.message.role === 'user' && JSON.stringify(row.message.content).includes(text)).length, 1);
  }
  await writeFile(options.output + '/scenario.json', JSON.stringify({ status: 'passed', state }, null, 2));
  process.stdout.write(JSON.stringify({ status: 'passed', models: state.model_turns, spent: state.budget.spent }) + '\n');
} finally {
  session.dispose();
  try { process.kill(hostPid, 'SIGTERM'); } catch {}
}
