import { join } from 'node:path';
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
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
import { projectInstructions } from './context-files.mjs';
import { workspaceTools } from './workspace-tools.mjs';
import { projectHarness } from './input.ts';
import { randomUUID, createHash } from 'node:crypto';
import { directRenderers, explorationBoundary } from './tool-render.mjs';
import { delegationPrompt } from './delegation-prompt.mjs';
import { OPERATOR_WORKFLOW_PROMPT } from './operator-workflow-prompt.mjs';

// Product themes (light/dark follow the terminal): no background blocks, warm accent, quiet borders.
const THEMES = ['kernelbridge-dark', 'kernelbridge-light'].map(name => fileURLToPath(new URL(`../../../themes/${name}.json`, import.meta.url)));

const SYSTEM = 'You are a kernel engineering agent. Use Pi file tools to inspect and implement the candidate. Use the configured evaluation, knowledge, and receipt tools for domain work. Treat task references as read-only. Only original evaluation receipts establish correctness or performance. After an error, inspect the returned evidence and revise the candidate.';

function providerDefinition(stream, modelInfo) {
  return { name: 'KernelCat controlled transport', api: 'openai-completions', baseUrl: 'https://invalid.example',
    apiKey: 'kernelcat-broker-placeholder', streamSimple: stream,
    models: [{ id: modelInfo?.name ?? 'controlled', name: modelInfo?.name ?? 'Controlled', input: ['text'], reasoning: false,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: modelInfo?.context_window ?? 2097152, maxTokens: modelInfo?.max_tokens ?? 65536 }] };
}

function directTools(hello, controller) {
  return hello.tools.filter(tool => hello.native_harness?.workspace?.mode === 'project'
    ? ['read_receipt', 'finish_goal', 'get_goal', 'update_goal', 'delegate', 'agents_status', 'stop_agent', ...(hello.project_tools ?? [])].includes(tool.name)
    : !['read_file', 'write_file', 'edit_file', RSI_TOOL, REPLAY_TOOL].includes(tool.name)).map(tool => ({
    name: tool.name, label: tool.name, description: tool.description, parameters: tool.parameters,
    exposure: 'model-only',
    promptSnippet: tool.description,
    ...directRenderers(tool.name),
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
  return assistantMessage({ text: reply.text, calls: reply.calls ?? [], usage: reply.usage ?? {} }, model, Date.now(), 'kernelcat_child_effect');
}

async function runtimeServices(options, dependencies, provider, factories, replay, replayOnCompaction, frozenInstructions, semanticCompaction) {
  const { sdk, InMemoryCredentialStore } = dependencies;
  const modelRuntime = await sdk.ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null,
    allowModelNetwork: false, refreshOnCreate: false });
  modelRuntime.registerProvider('kernelcat', provider);
  const settingsManager = sdk.SettingsManager.inMemory({ compaction: { enabled: replayOnCompaction || semanticCompaction },
    retry: { enabled: false, maxRetries: 0 }, quietStartup: options.project ? 'header' : true,
    theme: 'kernelbridge-light/kernelbridge-dark' }, { projectTrusted: false });
  const resourceLoader = new sdk.DefaultResourceLoader({ ...options, settingsManager,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    additionalThemePaths: THEMES,
    ...(replay?.history ? workspaceHistoryResources(replay.history, settingsManager) : {}),
    agentsFilesOverride: () => structuredClone(frozenInstructions),
    extensionFactories: factories, appendSystemPrompt: [options.project
      ? 'You are a coding assistant working in the user project. Read the project instructions, inspect relevant files, implement the requested changes, and verify them. Tools operate directly in the current project. Finish with a concise account of changes and checks. Do not start autonomous goals, trials, or device evaluations unless the user explicitly requests them.' : SYSTEM,
      ...(options.delegationPrompt ? [options.delegationPrompt] : []),
      ...(options.operatorWorkflow ? [OPERATOR_WORKFLOW_PROMPT] : [])] });
  await resourceLoader.reload();
  if (resourceLoader.getExtensions().errors.length) throw new Error('Required Pi extension failed to load');
  return { cwd: options.cwd, agentDir: options.agentDir, modelRuntime,
    settingsManager, resourceLoader, diagnostics: [] };
}

/** Only a reply made entirely of delegate calls runs its tools concurrently. */
function delegateBatches(session) {
  const state = session.agent.state;
  const descriptor = Object.getOwnPropertyDescriptor(state, 'tools');
  if (!descriptor?.set || !descriptor.get) throw new Error('Pi agent tool state is not configurable');
  const mark = tools => tools.map(tool => tool.name === 'delegate' ? tool : { ...tool, executionMode: 'sequential' });
  Object.defineProperty(state, 'tools', { configurable: true, enumerable: true,
    get: descriptor.get, set(next) { descriptor.set.call(state, mark(next)); } });
  state.tools = descriptor.get.call(state);
  session.agent.toolExecution = 'parallel';
}

function bindTurnCheckpoint(session, controller, continuation, replay, rsiRpc, parallelDelegation = false) {
  session.agent.toolExecution = 'sequential';
  if (parallelDelegation) delegateBatches(session);
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

export async function createGovernedRuntime(client, home, dependencies, options = {}) {
  const { sdk, createAssistantMessageEventStream, Type,
    createPluginBridge, childContext } = dependencies;
  let session;
  let nativeRuntime;
  let unsubscribe;
  try {
    // Persistent hosts run child model calls on their own threads; give each
    // delegation permit an ordered lane so independent children overlap.
    const rpc = serialRpc(client, { childLanes: options.persistent === true });
    const hello = await rpc.call('hello', { native_replay: true });
    const native = hello.native_harness?.schema_version === 'kernelcat.pi-harness/v1';
    if (hello.native_harness !== undefined && (!native || hello.native_harness.adapter !== 'pi@1.5.0' ||
        hello.native_harness.mcp_server !== 'kernelcat' ||
        !Array.isArray(hello.native_harness.native_tools) || hello.native_harness.native_tools.some(name =>
          !['read', 'write', 'edit', 'bash', 'grep', 'find', 'ls', 'codemode', 'tool_search'].includes(name)))) {
      throw new Error('Unsupported native Harness capability');
    }
    const project = hello.native_harness?.workspace?.mode === 'project';
    const replay = hello.replay === undefined ? undefined : await prepareProductReplay(hello.replay, { project });
    if (replay && !native) throw new Error('Persistent replay requires native Pi');
    const cwd = replay?.capability.workspace ?? hello.native_harness?.workspace?.root ?? home;
    const agentDir = replay?.agentDir ?? join(home, 'agent');
    process.env.PI_CODING_AGENT_DIR = agentDir;
    // The host renders child progress inside each delegate row. Keep the
    // community plugin's separate fleet/async widgets out of the product UI.
    mkdirSync(join(agentDir, 'extensions/subagent'), { recursive: true, mode: 0o700 });
    writeFileSync(join(agentDir, 'extensions/subagent/config.json'), JSON.stringify({ fleetView: false, asyncWidget: false }) + '\n', { mode: 0o600 });
    const frozenInstructions = projectInstructions(cwd);
    let bridge;
    let plugin;
    const receipts = native ? createReceiptContextBinding() : undefined;
    const controller = new RunController(rpc, native,
      (call, signal) => plugin.execute(call, signal), () => bridge?.isNested() === true, receipts,
      { persistent: options.persistent === true });
    const rsi = createRsiExtension({ capability: hello.rsi, native, tools: hello.tools,
      rpc, controller });
    const replayPlugin = createReplayExtension({ configuration: replay?.capability.plugin,
      native, tools: hello.tools, controller });
    const replayOnCompaction = options.semanticCompaction !== true && replayPlugin?.configuration.replay_on_compaction === true;
    function stream(model, context, streamOptions) {
      const events = createAssistantMessageEventStream();
      const partial = assistant({ text: '', calls: [] }, model);
      let streamed = false, open;
      // Allocate identity before requesting a model. Claiming the first global
      // broadcast races with concurrent children and background completions.
      const generation = randomUUID();
      const close = () => {
        if (!open) return;
        const block = partial.content[open.index];
        events.push(open.kind === 'thinking'
          ? { type: 'thinking_end', contentIndex: open.index, content: block.thinking, partial }
          : { type: 'text_end', contentIndex: open.index, content: block.text, partial });
        open = undefined;
      };
      const stopDeltas = options.subscribeModelDeltas?.((delta, id) => {
        if (id !== generation) return;
        const kind = delta.type === 'text_delta' ? 'text' : delta.type === 'thinking_delta' ? 'thinking' : null;
        if (!kind || !delta.delta) return;
        if (kind === 'text') explorationBoundary();
        if (!streamed) { partial.content = []; events.push({ type: 'start', partial }); streamed = true; }
        if (open?.kind !== kind) {
          close();
          partial.content.push(kind === 'text' ? { type: 'text', text: '' } : { type: 'thinking', thinking: '' });
          open = { kind, index: partial.content.length - 1 };
          events.push({ type: kind === 'text' ? 'text_start' : 'thinking_start', contentIndex: open.index, partial });
        }
        const block = partial.content[open.index];
        if (kind === 'text') block.text += delta.delta; else block.thinking += delta.delta;
        events.push({ type: kind === 'text' ? 'text_delta' : 'thinking_delta', contentIndex: open.index, delta: delta.delta, partial });
      });
      const generate = async () => {
        try {
          if (controller.failure) throw controller.failure;
          const child = childContext.getStore();
          let message;
          let hostStopped = false;
          if (nativeRuntime?.session.isCompacting && options.semanticCompaction) {
            const result = await rpc.call('application_compact', { input_id: randomUUID(), harness: projectHarness(context) });
            message = assistant(result, model);
          } else if (controller.stopped) {
            hostStopped = true;
            message = assistant({ text: 'Invocation stopped by host.', calls: [] }, model);
          } else if (child) {
            message = await childReply(rpc, child, model, assistant);
          } else {
            await options.beforeModel?.();
            message = assistant(await controller.model(context), model, Date.now(), native ? null : 'kernelcat_effect');
            hostStopped = controller.stopped;
          }
          // Admission can stop between a Goal continuation and its model call.
          // Retain that boundary without presenting host control as model prose.
          if (options.persistent && hostStopped && !streamed) {
            message.content = [];
            message.kernelbridgeHostStop = true;
          }
          if (streamed) close();
          if (message.content?.some(block => block.type === 'text' && block.text.trim())) explorationBoundary();
          events.push({ type: 'done', reason: message.stopReason, message });
          events.end(message);
        } catch (error) {
          if (!error.knownChildStop) controller.fail(error);
          const message = assistant({ text: 'Transport failed; no automatic retry.', calls: [] }, model);
          message.stopReason = streamOptions?.signal?.aborted ? 'aborted' : 'error';
          message.errorMessage = String(error);
          events.push({ type: 'error', reason: message.stopReason, error: message });
          events.end(message);
        } finally { stopDeltas?.(); }
      };
      void (options.withModelGeneration ? options.withModelGeneration(generation, generate) : generate());
      return events;
    }
    const provider = providerDefinition(stream, hello.model_info);
    const tools = native ? directTools(hello, controller) : [legacyTool(Type, controller)];
    async function createRuntime(runtimeOptions) {
      const continuation = createControlledContinuation(controller);
      const delegateSpec = hello.tools.find(tool => tool.name === 'delegate');
      plugin = createPluginBridge(rpc, provider, cwd, { maxConcurrent: hello.subagents?.max_concurrent ?? undefined,
        ...(delegateSpec ? { agents: delegateSpec.parameters.properties.agent.enum } : {}) });
      const bridgeTools = hello.tools.filter(tool => tool.name !== REPLAY_TOOL && (!rsi || tool.name !== RSI_TOOL));
      if (native) bridge = createNativeBridge(sdk, rpc, bridgeTools, home,
        (id, name, arguments_) => controller.sealedCall(id, name, arguments_), error => controller.fail(error), receipts, {
          tools: hello.native_harness.workspace ? workspaceTools(sdk,
            { ...hello.native_harness.workspace, root: cwd, tools: hello.native_harness.native_tools }) : [],
          codemode: hello.native_harness.native_tools.includes('codemode'), timeout: hello.native_harness.timeout,
          includeReceiptText: !project,
          onStarted: id => controller.started.add(id), onCompleted: id => controller.completed.add(id),
        });
      const factories = [...(options.extensionFactories ?? []), continuation.extension, controller.validationExtension(), ...plugin.factories, ...(bridge?.factories ?? []), ...(rsi ? [rsi] : []),
        ...(replayPlugin ? [replayPlugin.factory] : []),
        createReplayContextExtension({ rpc, fail: error => controller.fail(error), receipts,
          replayOnCompaction, semanticCompaction: options.semanticCompaction, isStopped: () => controller.stopped })];
      const services = await runtimeServices({ ...runtimeOptions, project,
        delegationPrompt: delegationPrompt(hello.tools),
        operatorWorkflow: hello.tools.some(tool => tool.name === 'pipeline_status') }, dependencies, provider, factories, replay, replayOnCompaction, frozenInstructions, options.semanticCompaction === true);
      const created = await sdk.createAgentSession({ ...runtimeOptions, ...services,
        model: services.modelRuntime.getModel('kernelcat', provider.models[0].id), noTools: 'builtin',
        // Delegation uses the admitted event bridge. Exclude the plugin's model
        // tools so session restore cannot activate an ungoverned second entry.
        excludeTools: ['subagent', 'subagents_enable', 'subagent_supervisor', 'contact_supervisor', 'intercom'],
        customTools: tools, ...(native ? {} : { tools: ['kernelcat_effect'] }) });
      bindTurnCheckpoint(created.session, controller, continuation, replay, rsi ? rpc : undefined, options.persistent === true);
      return { ...created, services, diagnostics: [] };
    }
    nativeRuntime = await sdk.createAgentSessionRuntime(createRuntime, { cwd, agentDir,
      sessionManager: options.sessionManager ?? (replay ? sdk.SessionManager.create(cwd, replay.sessions) : sdk.SessionManager.inMemory(cwd)),
      sessionStartEvent: { type: 'session_start', reason: 'startup' } });
    async function bind(current) {
      unsubscribe?.();
      unsubscribe = current.subscribe(event => options.onEvent?.(structuredClone(event)));
      if (!options.interactive) await current.bindExtensions({ onError: event => controller.fail(new Error(event.error ?? String(event))) });
      await options.onBind?.(current, controller);
      if (native) current.setActiveToolsByName([...hello.native_harness.native_tools, ...tools.map(tool => tool.name),
        ...(rsi ? [RSI_TOOL] : []), ...(replayPlugin ? [REPLAY_TOOL] : [])]);
    }
    nativeRuntime.setRebindSession(bind);
    await bind(nativeRuntime.session);
    const restored = replay ? await restoreProductSession(nativeRuntime, replay) : false;
    session = nativeRuntime.session;
    const runtime = new Proxy(nativeRuntime, {
      get(target, property) {
        if (property === 'setRebindSession') return rebind => target.setRebindSession(async current => {
          await bind(current);
          await rebind?.(current);
        });
        const value = Reflect.get(target, property, target);
        return typeof value === 'function' ? value.bind(target) : value;
      },
    });
    return {
      runtime, controller, hello, restored, sdk, home,
      manifest: { pi_version: '1.5.0',
        system_prompt_sha256: createHash('sha256').update(nativeRuntime.session.systemPrompt).digest('hex'),
        tools_sha256: createHash('sha256').update(JSON.stringify(nativeRuntime.session.getAllTools().map(({ name, description, parameters }) => ({ name, description, parameters })))).digest('hex'),
        resources: frozenInstructions.agentsFiles.map(file => ({ path: file.path,
          sha256: createHash('sha256').update(file.content).digest('hex') })) },
      get session() { return nativeRuntime.session; },
      async prompt(text = hello.goal, promptOptions = {}) {
        if (controller.failure) throw controller.failure;
        await nativeRuntime.session.prompt(text, { expandPromptTemplates: false, ...promptOptions });
        if (controller.failure) throw controller.failure;
      },
      async dispose() { unsubscribe?.(); await nativeRuntime.dispose(); },
      async checkpoint() {
        return replay ? snapshotProductSession(nativeRuntime.session, replay) : undefined;
      },
      async complete() { return await rpc.call('complete'); },
    };
  } catch (error) {
    unsubscribe?.();
    if (nativeRuntime) await nativeRuntime.dispose();
    throw error;
  }
}
