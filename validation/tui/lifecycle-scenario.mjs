import { StepTimeout, scaled } from './timing.mjs';
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { ApplicationSession } from '../../source/apps/agent/src/sdk/index.ts';
import { ApplicationClient } from '../../source/apps/agent/src/application/client.mjs';
import { WorkerLease } from '../../source/apps/agent/src/application/worker-lease.mjs';

const base = JSON.parse(await readFile(process.argv[2], 'utf8'));
const outcomes = [];
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate) {
  const end = Date.now() + scaled(30000);
  while (Date.now() < end) { if (await predicate()) return; await sleep(50); }
  throw new StepTimeout('Lifecycle transition did not settle within ' + scaled(30000) + ' ms');
}
for (const mode of ['pause', 'detach', 'awaiting_user', 'budget', 'credits', 'seed_credits']) {
  const output = base.output + '/' + mode;
  await mkdir(output);
  const task = JSON.parse(await readFile(base.task, 'utf8'));
  task.id += '-' + mode;
  const config = JSON.parse(await readFile(base.config, 'utf8'));
  config.state_dir = output + '/state'; config.workspace = output + '/workspace';
  config.providers.primary.model = mode;
  if (mode === 'budget') config.policy.max_steps = 2;
  if (mode === 'credits') config.credits = 7;
  if (mode === 'seed_credits') config.credits = 1;
  await writeFile(output + '/task.json', JSON.stringify(task));
  await writeFile(output + '/config.json', JSON.stringify(config));
  const session = await ApplicationSession.create({ ...base, output, task: output + '/task.json', config: output + '/config.json', sessionOnly: false,
    ...(mode === 'budget' ? { maxModelTurns: 5 } : {}) });
  let owner, control;
  const hostPid = (await session.state()).host_pid;
  try {
    await session.prompt('Run the deterministic local fixture.', { inputId: 'input' });
    owner = await ApplicationClient.connect(session.client.directory);
    const { ticket } = await session.client.call('ticket', { mode: 'interactive' });
    const lease = await WorkerLease.claim(owner, ticket);
    control = await ApplicationClient.connect(session.client.directory);
    await control.call('bind_worker', { lease: lease.lease });
    if (mode === 'budget') {
      await session.setGoal(task.goal, { acceptance: { evidence_tier: 'E3' } });
      assert.equal((await session.state()).task_version, 2);
    }
    if (mode !== 'pause') await control.call('detach', { on_detach: 'continue' });
    if (mode === 'awaiting_user') await session.interrupt();
    owner.close();
    await until(async () => (await session.state()).lease?.worker_id !== lease.lease.worker_id);
    if (['pause', 'awaiting_user'].includes(mode)) {
      const paused = await session.state();
      assert.equal(paused.goal.status, mode === 'pause' ? 'paused' : 'awaiting_user');
      assert.equal(paused.model_turns, 0);
      assert.equal(paused.lease, null);
      await session.resume();
    }
    await until(async () => (await session.state()).goal.status === (['budget', 'credits', 'seed_credits'].includes(mode) ? 'budget_limit' : 'completed'));
    await until(async () => !(await session.state()).lease);
    const completed = await session.state();
    if (['credits', 'seed_credits'].includes(mode)) {
      assert.equal(completed.evidence.length, 0);
      assert.equal(completed.budget.spent, config.credits);
      assert.equal(completed.budget.reserved, 0);
      assert.equal(completed.model_turns, mode === 'credits' ? 1 : 0);
    } else assert.equal(completed.evidence[0].evidence_tier, 'E1');
    assert.equal(completed.inputs.input.status, ['budget', 'credits', 'seed_credits'].includes(mode) ? 'failed' : 'completed',
      'an input stopped by an explicit limit must not be reported as completed');
    assert.equal(completed.pending_effect_ids.length, 0);
    assert.equal(completed.trials.length, mode === 'budget' ? 3 : 1);
    if (mode === 'budget') assert.equal(completed.model_turns, 5);
    outcomes.push({ mode, goal: completed.goal.status, models: completed.model_turns, spent: completed.budget.spent });
  } finally {
    owner?.close(); control?.close(); session.dispose();
    try { process.kill(hostPid, 'SIGTERM'); } catch {}
  }
}
await writeFile(base.output + '/scenario.json', JSON.stringify({ status: 'passed', outcomes }, null, 2));
process.stdout.write(JSON.stringify({ status: 'passed', outcomes }) + '\n');
