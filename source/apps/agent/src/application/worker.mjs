import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ApplicationClient } from './client.mjs';
import { WorkerLease } from './worker-lease.mjs';
import { controlledModels } from '../loopx/model-control.ts';
import { controlledRsi } from '../loopx/rsi-control.ts';
import { controlledContext } from '../loopx/context-control.ts';
import { withPreparedAgent } from '../harness/pi/sdk.mjs';
import { createHostActions } from '../harness/pi/host-actions.mjs';
import { runInteractive } from '../harness/pi/interactive-driver.mjs';
import { runHeadless } from '../harness/pi/headless-driver.mjs';
import { recoverApplication } from './recovery.mjs';

export async function runApplicationWorker(directory, ticket, { interactive = false } = {}) {
  const effectClient = await ApplicationClient.connect(directory);
  let control;
  const scratch = await mkdtemp(join(tmpdir(), 'kernelbridge-context-'));
  let actions;
  try {
    const lease = await WorkerLease.claim(effectClient, ticket);
    control = await ApplicationClient.connect(directory);
    await control.call('bind_worker', { lease: lease.lease });
    actions = createHostActions(control, lease);
    const rpc = controlledContext(controlledRsi(controlledModels(lease), async () => {
      const state = await control.state();
      return state.trials.at(-1)?.rsi_context;
    }), scratch);
    return await withPreparedAgent(async run => {
      await recoverApplication(lease);
      return run(rpc, {
      interactive, persistent: true, semanticCompaction: true, extensionFactories: [actions.extension],
      beforeModel: () => actions.beforeModel(),
      subscribeModelDeltas: listener => control.subscribe(event => {
        if (event.type === 'model_delta') listener(event.delta, event.generation_id);
      }),
      onEvent(event) {
        if (event.type !== 'message_update') void control.call('event', event).catch(() => {});
      },
      driver: governed => interactive ? runInteractive(governed, actions) : runHeadless(governed, actions),
      });
    });
  } finally {
    actions?.dispose();
    control?.close();
    effectClient.close();
    await rm(scratch, { recursive: true, force: true });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2);
    const directory = args[args.indexOf('--host') + 1];
    const ticket = args[args.indexOf('--ticket') + 1];
    if (!args.includes('--host') || !args.includes('--ticket')) throw new Error('Worker requires --host and its one-use --ticket');
    await runApplicationWorker(directory, ticket, { interactive: !args.includes('--headless') });
  } catch (error) { process.stderr.write(String(error) + '\n'); process.exitCode = 1; }
}
