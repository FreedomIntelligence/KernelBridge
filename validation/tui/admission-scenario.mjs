import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ApplicationSession } from '../../source/apps/agent/src/sdk/index.ts';
import { ApplicationClient } from '../../source/apps/agent/src/application/client.mjs';
import { WorkerLease } from '../../source/apps/agent/src/application/worker-lease.mjs';
import { recoverApplication } from '../../source/apps/agent/src/application/recovery.mjs';
import { controlledModels } from '../../source/apps/agent/src/loopx/model-control.ts';
import { controlledContext } from '../../source/apps/agent/src/loopx/context-control.ts';
import { withPreparedAgent } from '../../source/apps/agent/src/harness/pi/sdk.mjs';
import { createHostActions } from '../../source/apps/agent/src/harness/pi/host-actions.mjs';

const options = JSON.parse(await readFile(process.argv[2], 'utf8'));
const session = await ApplicationSession.create({ ...options, sessionOnly: false });
const hostPid = (await session.state()).host_pid;
const scratch = await mkdtemp(join(tmpdir(), 'kb-admission-'));
let owner, control, actions;
try {
  owner = await ApplicationClient.connect(session.client.directory);
  const { ticket } = await session.client.call('ticket', { mode: 'interactive' });
  const lease = await WorkerLease.claim(owner, ticket);
  control = await ApplicationClient.connect(session.client.directory);
  await control.call('bind_worker', { lease: lease.lease });
  await recoverApplication(lease);
  actions = createHostActions(control, lease);
  let injected = false, before;
  const gated = { async call(method, params) {
    // Deterministically put pause between prepare_turn and atomic model admission.
    if (method === 'model' && !injected) {
      injected = true;
      await session.pause();
      before = (await session.state()).budget;
    }
    return lease.call(method, params);
  } };
  await withPreparedAgent(run => run(controlledContext(controlledModels(gated), scratch), {
    persistent: true, semanticCompaction: true, extensionFactories: [actions.extension],
    beforeModel: () => actions.beforeModel(),
    driver: async governed => {
      await actions.bind(governed);
      await actions.headless();
      const paused = await session.state();
      assert.equal(injected, true);
      assert.equal(paused.goal.status, 'paused');
      assert.equal(paused.model_turns, 0);
      assert.deepEqual(paused.budget, before);
      assert.equal(paused.pending_effect_ids.length, 0);
      assert.equal(Object.keys(paused.calls).length, 0);
      assert.equal(governed.controller.failure, null);
      await session.resume();
      await actions.headless();
      const completed = await session.state();
      assert.equal(completed.goal.status, 'completed');
      assert.equal(completed.model_turns, 3);
      assert.equal(completed.pending_effect_ids.length, 0);
      assert.equal(Object.keys(completed.calls).length, 0);
      await writeFile(options.output + '/scenario.json', JSON.stringify({ status: 'passed', paused, completed }, null, 2));
      process.stdout.write(JSON.stringify({ status: 'passed', pausedModels: 0, resumedModels: completed.model_turns,
        spent: completed.budget.spent }) + '\n');
    },
  }));
} finally {
  actions?.dispose(); owner?.close(); control?.close(); session.dispose();
  await rm(scratch, { recursive: true, force: true });
  try { process.kill(hostPid, 'SIGTERM'); } catch {}
}
