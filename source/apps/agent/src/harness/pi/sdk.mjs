import { createGovernedRuntime } from './runtime-factory.mjs';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { withProcessEnvironment } from './environment.mjs';

async function withAgentEnvironment(home, execute) {
  const previous = { home: process.env.HOME, agent: process.env.PI_CODING_AGENT_DIR };
  process.env.HOME = home;
  process.env.PI_CODING_AGENT_DIR = join(home, 'agent');
  try { return await execute(); }
  finally {
    if (previous.home === undefined) delete process.env.HOME;
    else process.env.HOME = previous.home;
    if (previous.agent === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = previous.agent;
  }
}

/** Load the locked SDK before the callback can create a legacy effect worker. */
export async function withPreparedAgent(execute) {
  return withProcessEnvironment({}, () => prepareAgent(execute));
}

async function prepareAgent(execute) {
  const home = await mkdtemp(join(tmpdir(), 'kernelcat-pi-'));
  let running;
  try {
    const dependencies = await withAgentEnvironment(home, async () => {
      const sdk = await import('@earendil-works/pi-coding-agent');
      const { InMemoryCredentialStore, createAssistantMessageEventStream } = await import('@earendil-works/pi-ai');
      const { Type } = await import('typebox');
      const { createPluginBridge, childContext } = await import('./plugin.mjs');
      return { sdk, InMemoryCredentialStore, createAssistantMessageEventStream, Type, createPluginBridge, childContext };
    });
    // Restore the parent's environment before its callback spawns Python.
    return await execute((client, options) => {
      running = withAgentEnvironment(home, () => runPreparedAgent(client, home, dependencies, options));
      return running;
    });
  } finally {
    // The finite client may settle its stop boundary before SDK disposal finishes.
    // Keep its original error while waiting for the runner's owned cleanup.
    if (running) await Promise.allSettled([running]);
    await rm(home, { recursive: true, force: true });
  }
}

async function runPreparedAgent(client, home, dependencies, options = {}) {
  const governed = await createGovernedRuntime(client, home, dependencies, options);
  try {
    if (options.driver) return await options.driver(governed);
    await governed.prompt(governed.restored
      ? 'Continue from the restored conversation using the currently admitted tools and workspace. Current task and constraints:\n' + governed.hello.goal
      : governed.hello.goal);
  } finally { await governed.dispose(); }
  return await governed.complete();
}
