import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { assistant } from '../../clients/protocol.mjs';
import { serialRpc } from '../../clients/serial-rpc.ts';
import { RunController } from '../../run/controller.mjs';
import { createNativeBridge } from './native.mjs';
import { createControlledContinuation } from './continuation.mjs';
import { createRsiExtension, RSI_TOOL } from './rsi-extension.ts';
import { createReplayContextExtension } from './replay-context-extension.ts';
import { createReplayExtension, REPLAY_TOOL } from './replay-extension.ts';
import { prepareProductReplay, restoreProductSession, snapshotProductSession } from '../../replay/product-session.ts';
import { workspaceHistoryResources } from './workspace-history.ts';
import { createReceiptContextBinding } from '../../replay/receipt-context.ts';

const SYSTEM = 'KernelCat receipt-bound worker. TypeScript compiles checked journal projections; Host seals tool requests.';

function providerDefinition(stream) {
  return { name: 'KernelCat controlled transport', api: 'openai-completions', baseUrl: 'https://invalid.example',
    apiKey: 'kernelcat-broker-placeholder', streamSimple: stream,
    models: [{ id: 'controlled', name: 'Controlled', input: ['text'], reasoning: false,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 2097152, maxTokens: 65536 }] };
}

function directTools(hello, controller) {
  return hello.tools.filter(tool => ['finish', 'delegate'].includes(tool.name)).map(tool => ({
    name: tool.name, label: tool.name, description: tool.description, parameters: tool.parameters,
    exposure: 'model-only',
    execute: (id, arguments_, signal) => controller.tool(id, tool.name, arguments_, signal),
  }));
}

function legacyTool(Type, controller) {
  return { name: 'kernelcat_effect', label: 'KernelCat effect',
    description: 'Dispatch a Host-sealed tool call through the trusted Gateway.',
    parameters: Type.Object({}, { additionalProperties: true }),
    execute: (id, call, signal) => controller.tool(id, call.name, call.arguments, signal) };
}

async function childReply(rpc, child, model, assistantMessage) {
  const reply = await rpc.call('child_model_turn', { permit: child.permit });
  if (reply.failed) {
    const error = new Error(`Controlled child stopped: ${reply.reason ?? 'provider_failed'}`);
    error.knownChildStop = true;
    throw error;
  }
  return assistantMessage({ text: reply.text, calls: reply.calls ?? [] }, model, Date.now(), 'kernelcat_child_effect');
}

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
  const home = await mkdtemp(join(tmpdir(), 'kernelcat-pi-'));
  let running;
  try {
    const dependencies = await withAgentEnvironment(home, async () => {
      const sdk = await import('@earendil-works/pi-coding-agent');
      const { InMemoryCredentialStore, createAssistantMessageEventStream, Type } = await import('@earendil-works/pi-ai');
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

async function runtimeServices(options, dependencies, provider, factories, replay, replayOnCompaction) {
  const { sdk, InMemoryCredentialStore } = dependencies;
  const modelRuntime = await sdk.ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null,
    allowModelNetwork: false, refreshOnCreate: false });
  modelRuntime.registerProvider('kernelcat', provider);
  const settingsManager = sdk.SettingsManager.inMemory({ compaction: { enabled: replayOnCompaction },
    retry: { enabled: false, maxRetries: 0 } }, { projectTrusted: false });
  const resourceLoader = new sdk.DefaultResourceLoader({ ...options, settingsManager,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    ...(replay ? workspaceHistoryResources(replay.history, settingsManager) : {}),
    extensionFactories: factories, systemPrompt: SYSTEM });
  await resourceLoader.reload();
  if (resourceLoader.getExtensions().errors.length) throw new Error('Required Pi extension failed to load');
  return { cwd: options.cwd, agentDir: options.agentDir, modelRuntime,
    settingsManager, resourceLoader, diagnostics: [] };
}

function bindTurnCheckpoint(session, controller, continuation, replay, rsiRpc) {
  session.agent.toolExecution = 'sequential';
  const finalize = session.agent.finishTurn;
  session.agent.finishTurn = async (turn, signal) => {
    await finalize?.(turn, signal);
    const checkpoint = replay && controller.active && !controller.failure && !controller.stopped
      && !['error', 'aborted'].includes(turn.message.stopReason)
      ? await snapshotProductSession(session, replay) : undefined;
    const result = await continuation.finish(turn.message, checkpoint);
    // end_turn has acknowledged the original checkpoint. An interrupted node
    // write is reconciled from those receipts at the next native startup.
    if (checkpoint && rsiRpc) await rsiRpc.call('rsi_checkpoint');
    return result;
  };
}

async function runPreparedAgent(client, home, dependencies, options = {}) {
  const { sdk, createAssistantMessageEventStream, Type,
    createPluginBridge, childContext } = dependencies;
  let session;
  let nativeRuntime;
  let unsubscribe;
  try {
    const rpc = serialRpc(client);
    const hello = await rpc.call('hello', { native_replay: true });
    const native = hello.native_harness?.schema_version === 'kernelcat.pi-harness/v1';
    if (hello.native_harness !== undefined && (!native || hello.native_harness.adapter !== 'pi@1.0.0' ||
        hello.native_harness.mcp_server !== 'kernelcat' ||
        JSON.stringify(hello.native_harness.native_tools) !== JSON.stringify(['codemode', 'tool_search']))) {
      throw new Error('Unsupported native Harness capability');
    }
    const replay = hello.replay === undefined ? undefined : await prepareProductReplay(hello.replay);
    if (replay && !native) throw new Error('Persistent replay requires native Pi');
    const cwd = replay?.capability.workspace ?? home;
    const agentDir = replay?.agentDir ?? join(home, 'agent');
    process.env.PI_CODING_AGENT_DIR = agentDir;
    let bridge;
    let plugin;
    const receipts = native ? createReceiptContextBinding() : undefined;
    const controller = new RunController(rpc, native,
      (call, signal) => plugin.execute(call, signal), () => bridge?.isNested() === true, receipts);
    const rsi = createRsiExtension({ capability: hello.rsi, native, tools: hello.tools,
      rpc, controller });
    const replayPlugin = createReplayExtension({ configuration: replay?.capability.plugin,
      native, tools: hello.tools, controller });
    const replayOnCompaction = replayPlugin?.configuration.replay_on_compaction === true;
    function stream(model, context, streamOptions) {
      const events = createAssistantMessageEventStream();
      void (async () => {
        try {
          if (controller.failure) throw controller.failure;
          const child = childContext.getStore();
          let message;
          if (controller.stopped) {
            message = assistant({ text: 'Invocation stopped by host.', calls: [] }, model);
          } else if (child) {
            message = await childReply(rpc, child, model, assistant);
          } else {
            message = assistant(await controller.model(context), model, Date.now(), native ? null : 'kernelcat_effect');
          }
          events.push({ type: 'done', reason: message.stopReason, message });
          events.end(message);
        } catch (error) {
          if (!error.knownChildStop) controller.fail(error);
          const message = assistant({ text: 'Transport failed; no automatic retry.', calls: [] }, model);
          message.stopReason = streamOptions?.signal?.aborted ? 'aborted' : 'error';
          message.errorMessage = String(error);
          events.push({ type: 'error', reason: message.stopReason, error: message });
          events.end(message);
        }
      })();
      return events;
    }
    const provider = providerDefinition(stream);
    const tools = native ? directTools(hello, controller) : [legacyTool(Type, controller)];
    async function createRuntime(options) {
      const continuation = createControlledContinuation(controller);
      plugin = createPluginBridge(rpc, provider, home);
      const bridgeTools = hello.tools.filter(tool => tool.name !== REPLAY_TOOL && (!rsi || tool.name !== RSI_TOOL));
      if (native) bridge = createNativeBridge(sdk, rpc, bridgeTools, home,
        (id, name, arguments_) => controller.sealedCall(id, name, arguments_), error => controller.fail(error), receipts);
      const factories = [continuation.extension, ...plugin.factories, ...(bridge?.factories ?? []), ...(rsi ? [rsi] : []),
        ...(replayPlugin ? [replayPlugin.factory] : []),
        createReplayContextExtension({ rpc, fail: error => controller.fail(error), receipts,
          replayOnCompaction, isStopped: () => controller.stopped })];
      const services = await runtimeServices(options, dependencies, provider, factories, replay, replayOnCompaction);
      const created = await sdk.createAgentSession({ ...options, ...services,
        model: services.modelRuntime.getModel('kernelcat', 'controlled'), noTools: 'builtin',
        // Delegation uses the admitted event bridge. Exclude the plugin's model
        // tools so session restore cannot activate an ungoverned second entry.
        excludeTools: ['subagent', 'subagents_enable'],
        customTools: tools, ...(native ? {} : { tools: ['kernelcat_effect'] }) });
      bindTurnCheckpoint(created.session, controller, continuation, replay, rsi ? rpc : undefined);
      return { ...created, services, diagnostics: [] };
    }
    nativeRuntime = await sdk.createAgentSessionRuntime(createRuntime, { cwd, agentDir,
      sessionManager: replay ? sdk.SessionManager.create(cwd, replay.sessions) : sdk.SessionManager.inMemory(cwd),
      sessionStartEvent: { type: 'session_start', reason: 'startup' } });
    async function bind(current) {
      unsubscribe?.();
      unsubscribe = current.subscribe(event => options.onEvent?.(structuredClone(event)));
      await current.bindExtensions({ onError: event => controller.fail(new Error(event.error ?? String(event))) });
      if (native) current.setActiveToolsByName(['codemode', 'tool_search', ...tools.map(tool => tool.name),
        ...(rsi ? [RSI_TOOL] : []), ...(replayPlugin ? [REPLAY_TOOL] : [])]);
    }
    nativeRuntime.setRebindSession(bind);
    await bind(nativeRuntime.session);
    const restored = replay ? await restoreProductSession(nativeRuntime, replay) : false;
    session = nativeRuntime.session;
    await session.prompt(restored ? 'Continue the admitted task from this restored checkpoint. Preserve its goal and constraints; consult original receipts for completed work.' : hello.goal,
      { expandPromptTemplates: false });
    if (controller.failure) throw controller.failure;
    // Restoration requests are applied by the protected parent after native disposal.
    // Dispose Pi first so no live native callback can observe a changed workspace.
    try {
      await nativeRuntime.dispose();
    } finally {
      nativeRuntime = undefined;
      session = undefined;
    }
    if (controller.failure) {
      throw controller.failure;
    }
    return await rpc.call('complete');
  } finally {
    unsubscribe?.();
    if (nativeRuntime) await nativeRuntime.dispose();
    else if (session) {
      try { await session.extensionRunner.emit({ type: 'session_shutdown', reason: 'exit' }); }
      finally { session.dispose(); }
    }
  }
}
