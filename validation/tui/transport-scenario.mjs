import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { ApplicationSession } from '../../source/apps/agent/src/sdk/index.ts';

const options = JSON.parse(await readFile(process.argv[2], 'utf8'));
const session = await ApplicationSession.create(options);
const hostPid = (await session.state()).host_pid;
try {
  await session.prompt('First provider request fails.', { inputId: 'failed' });
  const failed = await session.run();
  assert.equal(failed.inputs.failed.status, 'failed');
  const deadline = Date.now() + 10000;
  while ((await session.state()).lease) {
    assert.ok(Date.now() < deadline);
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  const stopped = await session.state();
  assert.equal(stopped.model_turns, 1, 'no automatic provider retry');
  await session.prompt('Explicitly start a new turn after the settled failure.', { inputId: 'new-turn' });
  const resumed = await session.run();
  assert.equal(resumed.model_turns, 5);
  assert.equal(resumed.evidence.length, 1);
  assert.equal(resumed.pending_effect_ids.length, 0);
  assert.equal(resumed.inputs['new-turn'].status, 'completed');
  await writeFile(options.output + '/scenario.json', JSON.stringify({ status: 'passed', state: resumed }, null, 2));
  process.stdout.write(JSON.stringify({ status: 'passed', model_turns: resumed.model_turns, spent: resumed.budget.spent }) + '\n');
} finally {
  session.dispose();
  try { process.kill(hostPid, 'SIGTERM'); } catch {}
}
