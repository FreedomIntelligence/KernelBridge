import { randomUUID } from 'node:crypto';
import { open, mkdir, readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { InputQueue } from '../../application/input-queue.mjs';
import { workspaceTools } from './workspace-tools.mjs';
import { goalSummary, statePresentation, installProductUI, showProductFeedback } from './product-ui.mjs';
import { COMMAND_POLICY_VERSION, PRODUCT_COMMANDS, PRODUCT_COMMAND_NAMES, parseGoalCommand, COMMAND_HELP } from './command-policy.mjs';

export { COMMAND_POLICY_VERSION } from './command-policy.mjs';
const DISABLED = new Set(['/share', '/bug', '/debug']);
const DISPLAY = new Set(['/copy', '/name', '/session', '/changelog', '/hotkeys']);

/** All typed text crosses this gate before Pi's command/stream/compaction branches. */
export function createHostActions(control, effects) {
  const inputs = new InputQueue(control);
  let governed, context, state, activeInput;
  let applying = [];
  let persistedInputs = [];
  const runningInputs = new Set();
  let exiting = false;
  let approvedSwitch = false;
  let inputDelivery = Promise.resolve();
  let bashAbort;
  let interactive = false;
  let advancing = false;
  let delivered = new Set();
  let treeFork = false;
  const policyInputs = new Set();
  const syncTools = () => {
    if (governed && state.workspace_mode === 'project') {
      const tools = governed.session.getActiveToolNames().filter(name => name !== 'finish_goal');
      if (state.goal?.status === 'active') tools.push('finish_goal');
      governed.session.setActiveToolsByName(tools);
    }
  };
  const refresh = async () => {
    state = await control.state();
    syncTools();
    for (const receipt of Object.values(state.inputs ?? {})) {
      if (receipt.initiator === 'loopx' && receipt.kind === 'policy') policyInputs.add(receipt.text);
    }
    context?.ui.setStatus('kernelbridge', goalSummary(state));
    return state;
  };
  const stop = async () => {
    bashAbort?.abort();
    const goal = (await refresh()).goal;
    if (goal && ['active', 'paused', 'awaiting_user'].includes(goal.status)) await control.control('interrupt', { channel: 'tui' });
  };
  const extension = pi => {
    pi.on('session_start', async (_event, ctx) => {
      context = ctx;
      await refresh();
      if (interactive) installProductUI(context, () => state, { demo: process.env.KERNELBRIDGE_DEMO === '1' });
      if (governed && state.pending_transition) {
        const parent = state.pending_transition.parent;
        const ordinary = state.workspace_mode === 'project' && (!state.goal || ['cleared', 'completed'].includes(state.goal.status));
        if (!ordinary) governed.session.sessionManager.appendCustomMessageEntry('kernelbridge-retained-facts', JSON.stringify({
          goal: state.goal, task_version: state.task_version, budget: state.budget, best_accepted: state.best,
          single_best: state.single_best, platform: state.platform, pending_effect_ids: state.pending_effect_ids,
          user_constraints: Object.values(state.inputs).filter(item => item.initiator === 'user' && item.kind !== 'control').map(item => item.text),
        }), false);
        governed.session.refreshContext();
        if (ordinary) {
          await control.call('configuration', { operation: 'context_started', pi_session_id: ctx.sessionManager.getSessionId() });
        } else await control.call('trial', { trial_id: randomUUID(), source_sha256: parent.source_sha256,
          parent_entry_id: parent.entry_id, parent_pi_session_id: parent.pi_session_id,
          pi_session_id: ctx.sessionManager.getSessionId(), transition_id: state.pending_transition.transition_id });
        await refresh();
        await snapshot('checkpoint');
      }
    });
    pi.on('input', async event => {
      try {
        const receipt = inputs.consume(event.text);
        if (receipt.status !== 'queued') return { action: 'handled' };
        applying.push(receipt);
        activeInput = receipt;
        effects.bind({ input_id: receipt.input_id });
        await effects.call('application_enqueue', { input_id: receipt.input_id });
        if (governed) { governed.controller.stopped = false; governed.controller.stopResult = undefined; }
        return { action: 'continue' };
      } catch (error) {
        context?.ui.notify(String(error), 'error');
        return { action: 'handled' };
      }
    });
    pi.on('message_end', async (event, ctx) => {
      const manager = ctx.sessionManager;
      const identity = { pi_session_id: manager.getSessionId(), entry_id: manager.getLeafId() };
      effects.bind(identity);
      if (event.message.role === 'user' && applying.length) {
        const receipt = applying.shift();
        // Pi emits this hook BEFORE appendMessage. The marker identifies the next
        // user entry; only beforeModel may acknowledge the persisted message.
        pi.appendEntry('kernelbridge.input', { input_id: receipt.input_id });
        persistedInputs.push(receipt);
      }
    });
    pi.on('tool_call', event => {
      effects.bind({ tool_call_id: event.toolCallId, tool_name: event.toolName,
        tool_arguments: event.input, pi_session_id: context.sessionManager.getSessionId(),
        entry_id: context.sessionManager.getLeafId() });
    });
    pi.on('agent_settled', async event => {
      const providerFailed = governed?.controller.stopResult?.status === 'providers_failed';
      const setupFailed = governed?.controller.stopResult?.status === 'seed_failed';
      const limitReached = ['budget_exhausted', 'budget_limit', 'trial_limit', 'step_limit'].includes(governed?.controller.stopResult?.status);
      if (limitReached) showProductFeedback(context, '已达到显式设置的执行上限。当前请求已停止，未再调用模型。', true);
      if (providerFailed) {
        await effects.call('end_failed_turn', { native_checkpoint: await governed.checkpoint() });
      }
      if (event.aborted && governed) {
        const interrupted = await effects.call('interrupt_turn');
        const manager = governed.session.sessionManager;
        for (const item of interrupted.calls) {
          const branch = manager.getBranch();
          const paired = branch.some(entry => entry.type === 'message' && entry.message.role === 'toolResult' && entry.message.toolCallId === item.call.id);
          const assistant = branch.some(entry => entry.type === 'message' && entry.message.role === 'assistant'
            && entry.message.content.some(block => block.type === 'toolCall' && block.id === item.call.id));
          if (!paired && assistant) {
            const message = { role: 'toolResult', toolCallId: item.call.id, toolName: item.call.name,
              content: [{ type: 'text', text: JSON.stringify(item.receipt) }], isError: true, timestamp: Date.now() };
            manager.appendMessage(message);
            governed.session.agent.state.messages = [...governed.session.agent.state.messages, message];
          } else if (!assistant) {
            await governed.session.sendCustomMessage({ customType: 'kernelbridge-recovered',
              content: JSON.stringify({ original_call: item.call, receipt: item.receipt, recovered: true }), display: true });
          }
        }
        if (interrupted.needs_checkpoint) {
          const checkpoint = await governed.checkpoint();
          await effects.call('end_turn', { proposal_id: interrupted.proposal_id,
            ...(checkpoint ? { native_checkpoint: checkpoint } : {}) });
        }
        governed.controller.active = null;
        governed.controller.stopped = true;
        await stop();
      }
      for (const inputId of [...runningInputs]) {
        await control.call('transition', { input_id: inputId,
          status: event.aborted ? 'interrupted' : governed?.controller.failure || providerFailed || setupFailed || limitReached ? 'failed' : 'completed' });
        runningInputs.delete(inputId);
      }
      activeInput = undefined;
      if (governed) await snapshot('checkpoint');
      await refresh();
      if (interactive && !event.aborted && !exiting) setImmediate(() => { void advance().catch(report); });
    });
    pi.on('user_bash', async event => {
      const text = (event.excludeFromContext ? '!!' : '!') + event.command;
      const receipt = inputs.consume(text);
      pi.appendEntry('kernelbridge.input', { input_id: receipt.input_id, kind: 'bash' });
      const manager = governed.session.sessionManager;
      await persistInput(receipt, manager.getLeafId());
      await snapshot('checkpoint');
      const params = { input_id: receipt.input_id, command: event.command };
      const admitted = await effects.call('application_user_bash', params);
      if (admitted.cached) {
        await control.call('transition', { input_id: receipt.input_id, status: 'completed' });
        return { result: admitted.outcome.data.result?.structuredContent ?? { output: JSON.stringify(admitted.outcome), exitCode: 1, cancelled: false, truncated: false } };
      }
      const capability = governed.hello.native_harness.workspace;
      const bash = workspaceTools(governed.sdk, { ...capability, tools: ['bash'] })[0];
      let result;
      bashAbort = new AbortController();
      try {
        const output = await bash.execute(receipt.input_id, { command: event.command }, bashAbort.signal,
          update => { void control.call('event', { type: 'tool_execution_update', toolCallId: receipt.input_id, toolName: 'bash', partialResult: update }).catch(report); });
        result = { output: output.content.filter(item => item.type === 'text').map(item => item.text).join('\n'),
          exitCode: output.structuredContent?.exit_code ?? (output.isError ? 1 : 0), cancelled: false, truncated: false };
      } catch (error) { result = { output: String(error), exitCode: 1, cancelled: bashAbort.signal.aborted, truncated: false }; }
      bashAbort = undefined;
      await effects.call('application_user_bash', { ...params, result, status: result.exitCode === 0 ? 'completed' : 'failed' });
      await control.call('transition', { input_id: receipt.input_id, status: result.cancelled ? 'interrupted' : result.exitCode === 0 ? 'completed' : 'failed' });
      runningInputs.delete(receipt.input_id);
      await snapshot('checkpoint');
      return { result };
    });
    pi.on('session_before_switch', () => !governed || approvedSwitch ? undefined : { cancel: true });
    pi.on('session_before_fork', async event => {
      try {
        await snapshot('fork', event.entryId, event.position);
      } catch (error) { context.ui.notify(String(error), 'error'); return { cancel: true }; }
    });
    pi.on('session_before_tree', event => {
      // Cancel in-place tree mutation; execute a public fork after this hook returns.
      if (!treeFork) {
        treeFork = true;
        setImmediate(() => { void governed.runtime.fork(event.preparation.targetId, { position: 'at' })
          .catch(report).finally(() => { treeFork = false; }); });
      }
      return { cancel: true };
    });
    pi.on('session_before_compact', async () => {
      const current = await refresh();
      if (current.pending_effect_ids.length) return { cancel: true };
    });
    pi.on('session_compact', async () => { if (governed) await snapshot('checkpoint'); });
    pi.on('session_info_changed', async () => { if (governed && !governed.session.isStreaming) await snapshot('checkpoint'); });
  };

  async function snapshot(operation, entryId, position) {
    const manager = governed.session.sessionManager;
    if (!manager.getLeafId()) return;
    const native_checkpoint = await governed.checkpoint();
    return control.call('snapshot', { operation, entry_id: entryId ?? manager.getLeafId(), position,
      pi_session_id: manager.getSessionId(), session_file: manager.getSessionFile(), native_checkpoint });
  }

  const report = error => { showProductFeedback(context, String(error), true); };

  async function persistInput(receipt, entryId) {
    const manager = governed.session.sessionManager;
    const path = manager.getSessionFile();
    if (!path) throw new Error('Applied input has no persistent native session file');
    let native_checkpoint;
    try {
      const file = await open(path, 'r');
      try { await file.sync(); } finally { await file.close(); }
      const directory = await open(dirname(path), 'r');
      try { await directory.sync(); } finally { await directory.close(); }
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      native_checkpoint = await governed.checkpoint();
    }
    await control.call('transition', { input_id: receipt.input_id, status: 'applied',
      pi_session_id: manager.getSessionId(), entry_id: entryId, ...(native_checkpoint ? { native_checkpoint } : {}) });
    runningInputs.add(receipt.input_id);
  }

  async function resetContext() {
    if (state.workspace_mode === 'project' && !governed.session.sessionManager.getLeafId()) {
      approvedSwitch = true;
      try { await governed.runtime.newSession(); } finally { approvedSwitch = false; }
      return;
    }
    const captured = await snapshot('reset');
    if (!captured) throw new Error('No source/context boundary to reset');
    approvedSwitch = true;
    try {
      const ordinary = state.workspace_mode === 'project' && (!state.goal || ['cleared', 'completed'].includes(state.goal.status));
      await governed.runtime.newSession(ordinary ? {} : { setup: async manager => {
        manager.appendCustomMessageEntry('kernelbridge-handoff', JSON.stringify(captured.handoff), false);
      } });
    } finally { approvedSwitch = false; }
  }

  async function advance() {
    if (advancing || exiting || !governed || governed.session.isStreaming || governed.session.isCompacting
        || applying.length || persistedInputs.length || runningInputs.size) return false;
    advancing = true;
    try {
      const decision = await control.call('next');
      if (!decision.receipt) return false;
      if (decision.action === 'reset') await resetContext();
      if (decision.action === 'fork') await governed.runtime.fork(decision.receipt.target_entry_id, { position: 'at' });
      await deliver(decision.receipt);
      return true;
    } finally { advancing = false; }
  }

  async function deliver(receipt) {
    if (receipt.status !== 'queued' || !governed || delivered.has(receipt.input_id)) return;
    if (receipt.kind === 'prompt' && governed.session.isStreaming) return;
    delivered.add(receipt.input_id);
    if (receipt.kind === 'control') {
      await actions.submit(receipt.text, { streaming: governed.session.isStreaming, compacting: governed.session.isCompacting }, receipt);
      return;
    }
    inputs.enqueue(receipt);
    if (receipt.initiator === 'loopx' && receipt.kind === 'policy') policyInputs.add(receipt.text);
    const streamingBehavior = receipt.kind === 'follow_up' ? 'followUp' : 'steer';
    await governed.prompt(receipt.text, { source: 'rpc', ...(governed.session.isStreaming ? { streamingBehavior } : {}) });
  }

  const unsubscribe = control.subscribe(event => {
    if (event.type === 'state') {
      state = event.state;
      syncTools();
      context?.ui.setStatus('kernelbridge', goalSummary(state));
    } else if (event.type === 'input' && event.receipt.channel !== 'tui') {
      // prompt() resolves after a run when idle. Do not serialize later steering
      // behind that promise; Pi owns its queue and the host owns durable order.
      const delivery = deliver(event.receipt).catch(report);
      inputDelivery = Promise.all([inputDelivery, delivery]);
    } else if (event.type === 'control' && ['interrupt', 'pause'].includes(event.operation)) {
      if (event.operation === 'interrupt') void governed?.session.abort();
    } else if (event.type === 'control' && ['resume', 'set'].includes(event.operation)) {
      if (interactive) setImmediate(() => { void advance().catch(report); });
    } else if (event.type === 'takeover') {
      exiting = true;
    } else if (event.type === 'disconnected') {
      if (governed) { governed.controller.fail(new Error(event.error)); void governed.session.abort(); }
    }
  });

  const actions = {
    extension, inputs,
    commands: () => PRODUCT_COMMANDS.filter(command => state?.workspace_mode !== 'project'
      || !['import', 'detach', 'changelog', 'login', 'logout'].includes(command.name)).map(command => ({ ...command,
        ...(state?.workspace_mode === 'project' && command.name === 'new' ? { description: '开始新对话，保留工作文件' } : {}) })),
    presentInput: text => policyInputs.has(text)
      && !Object.values(state?.inputs ?? {}).some(receipt => receipt.initiator === 'user' && receipt.text === text)
      ? 'Goal 自动继续' : text,
    feedback: (message, error) => showProductFeedback(context, message, error),
    async beforeModel() {
      const manager = governed.session.sessionManager;
      const entries = manager.getBranch();
      const pendingInputs = persistedInputs;
      persistedInputs = [];
      for (const receipt of pendingInputs) {
        const marker = entries.findIndex(entry => entry.type === 'custom' && entry.customType === 'kernelbridge.input' && entry.data?.input_id === receipt.input_id);
        const entry = entries.slice(marker + 1).find(item => item.type === 'message' && item.message.role === 'user');
        if (marker < 0 || !entry) throw new Error('Input has no persisted marker/user entry pair');
        await persistInput(receipt, entry.id);
        await snapshot('checkpoint', entry.id);
      }
      const checkpoint = await snapshot('checkpoint');
      if (!state.trials.length && (state.workspace_mode !== 'project' || state.goal?.status === 'active')) {
        await control.call('trial', { trial_id: randomUUID(), pi_session_id: manager.getSessionId(),
          source_sha256: checkpoint.source_sha256 });
        await refresh();
      }
    },
    async bind(value, options = {}) {
      governed = value;
      interactive = options.interactive === true;
      await control.observe();
      const patch = JSON.parse(await readFile(new URL('../../../patches/pi-coding-agent-1.5.0-host-actions.json', import.meta.url), 'utf8'));
      await control.call('configuration', { operation: 'runtime_manifest', ...governed.manifest,
        command_policy_version: COMMAND_POLICY_VERSION, patch_sha256: patch.patch_sha256 });
      await refresh();
    },
    async initial() {
      await refresh();
      for (const receipt of Object.values(state.inputs).filter(item => item.status === 'queued' && item.kind === 'control')) await deliver(receipt);
      const entries = governed.session.sessionManager.getBranch();
      for (const receipt of Object.values(state.inputs).filter(item => item.status === 'queued' && item.kind !== 'control')) {
        const marker = entries.findIndex(entry => entry.type === 'custom' && entry.customType === 'kernelbridge.input' && entry.data?.input_id === receipt.input_id);
        const user = marker < 0 ? undefined : entries.slice(marker + 1).find(entry => entry.type === 'message' && entry.message.role === 'user');
        if (user) {
          await persistInput(receipt, user.id);
          await control.call('transition', { input_id: receipt.input_id, status: 'interrupted' });
          runningInputs.delete(receipt.input_id);
        }
      }
      await refresh();
      const queued = Object.values(state.inputs).filter(item => item.status === 'queued' && item.kind !== 'control');
      if (!queued.length && !governed.restored && Object.keys(state.inputs).length === 0 && state.workspace_mode !== 'project') {
        queued.push(await inputs.accept(governed.hello.goal, 'prompt', 'tui'));
      }
      if (queued.length) {
        inputs.enqueue(queued[0]);
        delivered.add(queued[0].input_id);
        return queued[0].text;
      }
    },
    async submit(raw, status, acceptedCommand) {
      const text = raw.trim().replace(/^\/exit(?=\s|$)/, '/quit').replace(/^\/clear(?=\s|$)/, '/new');
      if (!text) return { action: 'handled' };
      const [command, ...arguments_] = text.split(/\s+/);
      if (!text.startsWith('/')) {
        if (text.startsWith('!') && !text.replace(/^!!?/, '').trim()) return { action: 'handled' };
        const normalized = text.startsWith('!') ? (text.startsWith('!!') ? '!!' : '!') + text.replace(/^!!?/, '').trim() : text;
        const receipt = await inputs.accept(normalized, status.streaming || status.compacting ? 'steer' : 'prompt');
        if (receipt.status !== 'queued') return { action: 'handled', message: 'Original input was already applied' };
        // Native submission owns this delivery before agent_settled/next can
        // observe the durable queued receipt. Restart recovery still owns any
        // receipt that was accepted but never persisted by Pi.
        delivered.add(receipt.input_id);
        return { action: 'continue', text: normalized };
      }
      if (DISABLED.has(command)) return { action: 'reject', message: `${command} is disabled by the host profile` };
      if (!actions.commands().some(item => '/' + item.name === command)) return { action: 'reject', message: `不可用的命令：${command}。输入 /help 查看支持的命令。` };
      if (command === '/help' || command === '/hotkeys') return { action: 'handled', message: state.workspace_mode === 'project'
        ? '输入任务后执行，完成后等待下一条输入。\n/new 或 /clear：新对话（保留文件）· /model：模型 · /compact：压缩上下文\n/resume：历史会话 · /goal 目标内容：持续执行\nEsc：中断 · Ctrl+O：工具详情 · /quit 或 /exit：退出' : COMMAND_HELP };
      if (command === '/latest') return { action: 'handled' };
      if (command === '/goal') {
        const { operation, ...values } = parseGoalCommand(text);
        if (operation === 'status') return { action: 'handled', message: statePresentation(await refresh()) };
        await control.control(operation, { channel: 'tui', ...values,
          ...(acceptedCommand ? { input_id: acceptedCommand.input_id + '-goal' } : {}) });
        return { action: 'handled', message: statePresentation(await refresh()) };
      }
      if (command === '/detach') {
        if (governed.session.isStreaming || governed.session.isBashRunning) {
          return { action: 'reject', message: 'Wait for the current operation or interrupt it before detaching' };
        }
        await control.call('detach', { on_detach: 'continue' });
        exiting = true;
        return { action: 'continue', text: '/quit' };
      }
      if (command === '/quit') return { action: 'continue', text: '/quit' };
      if (command === '/settings') {
        return { action: 'handled', message: statePresentation(await refresh()) };
      }
      if (command === '/trust') {
        return { action: 'handled', message: `已冻结 ${governed.manifest.resources.length} 份任务指令。/reload 只重新加载这组资源。` };
      }
      if (command === '/scoped-models') {
        const result = await control.call('configure', { kind: 'model' });
        return { action: 'handled', message: `当前模型路由：${result.current}\n允许的路由：${result.allowed_routes.join('、')}` };
      }
      if (command === '/model' || command === '/thinking') {
        const result = await control.call('configure', { kind: command.slice(1), value: arguments_[0] });
        if (command === '/model') {
          const selected = arguments_[0] ? await control.call('configure', { kind: 'model' }) : result;
          return { action: 'handled', message: `当前模型路由：${selected.current}\n允许的路由：${selected.allowed_routes.join('、')}` };
        }
        return { action: 'handled', message: arguments_[0] ? `推理等级：${arguments_[0]}` : Object.entries(result.providers)
          .map(([name, options]) => `${name}：${options.thinking ?? options.reasoning_effort ?? '默认'}`).join('\n') };
      }
      if (command === '/reload') {
        await governed.runtime.services.resourceLoader.reload();
        return { action: 'handled', message: 'Reloaded the frozen resource set' };
      }
      if (command === '/login' || command === '/logout') {
        return { action: 'handled', message: 'Credentials are configured on the host through the profile api_key_env. Reconfigure the host to change credentials.' };
      }
      if (command === '/export') {
        const directory = join(state.output, 'exports');
        await mkdir(directory, { recursive: true, mode: 0o700 });
        return { action: 'continue', text: `/export ${join(directory, `session-${Date.now()}.html`)}` };
      }
      if (command === '/context' && arguments_[0] === 'clean') {
        await control.call('configuration', { operation: 'context_clean', rule: 'receipt-aware' });
        return { action: 'handled', message: 'Receipt-aware cleanup applies at the next context boundary; original messages are retained' };
      }
      if ((command === '/context' && arguments_[0] === 'reset') || command === '/new') {
        await resetContext();
        return { action: 'handled', message: state.workspace_mode === 'project'
          ? '已开始新对话，工作文件保留。' : 'Created a new context with the retained source and receipt handoff' };
      }
      if (command === '/clone') {
        await snapshot('checkpoint');
        await governed.runtime.fork(governed.session.sessionManager.getLeafId(), { position: 'at' });
        return { action: 'handled' };
      }
      if (command === '/compact') {
        try { await governed.session.compact(arguments_.join(' ') || undefined); }
        catch (error) {
          if (/Nothing to compact \(session too small\)/.test(String(error))) {
            return { action: 'handled', message: '上下文很短，无需压缩。' };
          }
          throw error;
        }
        return { action: 'handled' };
      }
      if (command === '/fork' && arguments_[0]) {
        await governed.runtime.fork(arguments_[0], { position: 'at' });
        return { action: 'handled' };
      }
      if (command === '/resume' || command === '/import') {
        const selected = await control.call('select_session', { path: arguments_[0] });
        if (selected.sessions) return { action: 'handled', message: JSON.stringify(selected) };
        approvedSwitch = true;
        try { await governed.runtime.switchSession(selected.path); }
        finally { approvedSwitch = false; }
        return { action: 'handled' };
      }
      if (command === '/compact' || command === '/fork' || command === '/clone' || command === '/tree' || DISPLAY.has(command)) {
        return { action: 'continue' };
      }
      return { action: 'reject', message: `Unknown or unadapted command: ${command}` };
    },
    interrupt: stop,
    resumeCommand: () => `kernelagent tui --attach ${control.applicationSessionId}`,
    async exit() {
      if (!exiting) {
        if (governed.session.isStreaming || governed.session.isBashRunning) {
          context?.ui.notify('Wait for the current operation or interrupt it before quitting', 'warning');
          return false;
        }
        exiting = true;
        await refresh();
        context?.ui.notify(goalSummary(state), 'info');
        await control.call('detach');
      }
      return true;
    },
    async headless() {
      const initial = await this.initial();
      if (initial) await governed.prompt(initial);
      await inputDelivery;
      while (await advance()) await inputDelivery;
    },
    dispose() { unsubscribe(); },
  };
  const routeSubmit = actions.submit;
  actions.submit = async (raw, status, acceptedCommand) => {
    if (!raw.trim().startsWith('/') || DISABLED.has(raw.trim().split(/\s+/)[0])) return routeSubmit(raw, status);
    const receipt = acceptedCommand ?? await inputs.accept(raw.trim(), 'control');
    if (receipt.status !== 'queued') return { action: 'handled', message: 'Original command was already acknowledged' };
    try {
      const result = await routeSubmit(raw, status, receipt);
      if (result.action === 'continue' && receipt.channel !== 'tui') throw new Error('This command requires the native interactive interface');
      await control.call('transition', { input_id: receipt.input_id, status: result.action === 'reject' ? 'failed' : 'completed',
        boundary: result.action === 'continue' ? 'native_command_dispatched' : 'host_command_completed' });
      return result;
    } catch (error) {
      await control.call('transition', { input_id: receipt.input_id, status: 'failed', error: String(error) });
      throw error;
    }
  };
  return actions;
}
