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
  let control, observer;
  const scratch = await mkdtemp(join(tmpdir(), 'kernelbridge-context-'));
  let actions;
  try {
    const lease = await WorkerLease.claim(effectClient, ticket);
    control = await ApplicationClient.connect(directory);
    await control.call('bind_worker', { lease: lease.lease });
    // Streaming observations must not delay or disconnect command replies.
    observer = await ApplicationClient.connect(directory);
    actions = createHostActions(control, lease, { observer });
    const rpc = controlledContext(controlledRsi(controlledModels(lease), async () => {
      const state = await control.state();
      return state.trials.at(-1)?.rsi_context;
    }), scratch);
    return await withPreparedAgent(async run => {
      await recoverApplication(lease);
      return run(rpc, {
      interactive, persistent: true, semanticCompaction: true, extensionFactories: [actions.extension],
      beforeModel: () => actions.beforeModel(),
      withModelGeneration: (generation, run) => lease.withModelGeneration(generation, run),
      subscribeModelDeltas: listener => observer.subscribe(event => {
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
    observer?.close();
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
  } catch (error) {
    // Keep the originating stack (and any transport cause) when startup fails.
    // String(error) hid the only evidence for failures before a session exists.
    console.error(error);
    process.exitCode = 1;
  }
}
