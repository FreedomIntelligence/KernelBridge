import { randomUUID } from 'node:crypto';
import { open, mkdir, readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { InputQueue } from '../../application/input-queue.mjs';
import { workspaceTools } from './workspace-tools.mjs';
import { continuationLabel, goalSummary, statePresentation, installProductUI, showProductFeedback, workingMessage } from './product-ui.mjs';
import { DETERMINISTIC, updateSubagentProgress, explorationBoundary, seedExplorationGroups } from './tool-render.mjs';
import { agentsPanel, BUILTIN_AGENTS } from './agents-panel.mjs';
import { COMMAND_POLICY_VERSION, PRODUCT_COMMANDS, PRODUCT_COMMAND_NAMES, parseGoalCommand, commandHelp } from './command-policy.mjs';
import { sessions } from '../../application/host.mjs';
import { sessionPicker } from '../../application/session-picker.mjs';
import { projectInfo } from '../../application/project-info.mjs';

export { COMMAND_POLICY_VERSION } from './command-policy.mjs';

/** What the working row says while a tool runs (Claude Code shows the active step). */
const TOOL_ACTIVITY = { bash: '运行命令', read: '读取文件', grep: '搜索代码', find: '查找文件', ls: '浏览目录',
  edit: '修改文件', write: '写入文件', delegate: '委派子代理' };
const SPINNER = ['·', '✢', '✳', '✶', '✻', '✽', '✻', '✶', '✳', '✢'];

/** Transcript order of text and tool calls, for presentation grouping only. */
function transcriptBlocks(entries) {
  const blocks = [];
  const results = new Map(entries.filter(entry => entry.type === 'message' && entry.message.role === 'toolResult')
    .map(entry => [entry.message.toolCallId, entry.message.isError ? 'failed' : 'completed']));
  for (const entry of entries) {
    if (entry.type !== 'message') continue;
    const { role, content } = entry.message;
    if (role === 'user') { blocks.push({ type: 'boundary' }); continue; }
    if (role !== 'assistant' || !Array.isArray(content)) continue;
    for (const block of content) {
      if (block.type === 'text' && block.text?.trim()) blocks.push({ type: 'boundary' });
      else if (block.type === 'toolCall') blocks.push({ type: 'tool', id: block.id, name: block.name, status: results.get(block.id) ?? 'completed' });
    }
  }
  return blocks;
}
const DISABLED = new Set(['/share', '/bug', '/debug']);
const DISPLAY = new Set(['/copy', '/name', '/session', '/changelog', '/hotkeys']);

/** All typed text crosses this gate before Pi's command/stream/compaction branches. */
export function createHostActions(control, effects, { observer = control } = {}) {
  const inputs = new InputQueue(control);
  let governed, context, state, activeInput;
  let applying = [];
  let persistedInputs = [];
  const runningInputs = new Set();
  // Turn settlement (input transitions and the boundary checkpoint) must finish before
  // the next input is applied; otherwise a queued notification's __input__ admission
  // can land between them and the host refuses the checkpoint as unsettled.
  let settlement = Promise.resolve();
  let exiting = false;
  let approvedSwitch = false;
  let inputDelivery = Promise.resolve();
  let bashAbort;
  let interactive = false;
  let advancing = false;
  let delivered = new Set();
  let treeFork = false;
  let observationFallback;
  let observationTimer;
  let disposed = false;
  let runGoalId = null;
  let userInterrupted = false;
  const policyInputs = new Set();
  let consecutiveErrors = 0;
  let compactions = 0;
  const forkHints = new Set();
  let shownReview;
  const suggestFork = async (reason, key) => {
    if (!state || state.fork_policy?.enabled || state.fork_policy?.suggest === false || forkHints.has(key)) return;
    forkHints.add(key);
    const text = `${reason} 可用 /fork 保留当前对话并另开思路，或用 /compact 保留摘要继续。分支前保留假设、已验收源码、失败条件和原任务 handle。`;
    await control.call('configuration', { operation: 'fork_suggestion', reason, key });
    showProductFeedback(context, text);
  };
  const syncTools = () => {
    if (governed && state.workspace_mode === 'project') {
      const tools = governed.session.getActiveToolNames().filter(name => !['finish_goal', 'update_goal', 'update_plan'].includes(name));
      // Pi captures its tool schema before beforeModel persists the new user
      // entry. That entry reactivates an awaiting Goal in the host journal.
      if (['active', 'awaiting_user'].includes(state.goal?.status)) tools.push('finish_goal', 'update_goal', 'update_plan');
      governed.session.setActiveToolsByName(tools);
    }
  };
  const acceptState = next => {
    if (!state || !Number.isSafeInteger(state.cursor) || !Number.isSafeInteger(next.cursor) || next.cursor >= state.cursor) state = next;
    return state;
  };
  const refresh = async () => {
    acceptState(await control.state());
    if (state.goal_review && state.goal_review.action_id !== shownReview) {
      shownReview = state.goal_review.action_id;
      showProductFeedback(context, `Goal 复核：${state.goal_review.status} · ${state.goal_review.summary}`);
    }
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
      seedExplorationGroups(transcriptBlocks(ctx.sessionManager.getBranch()));
      // Hide Pi's own footer until the host state is known (no startup flash).
      if (interactive) ctx.ui.setFooter(() => ({ invalidate() {}, render: () => [] }));
      await refresh();
      if (interactive) {
        installProductUI(context, () => state, { demo: process.env.KERNELBRIDGE_DEMO === '1' });
        context.ui.setHiddenThinkingLabel?.('思考中…');
        const accent = text => context.ui.theme?.fg?.('accent', text) ?? text;
        context.ui.setWorkingIndicator?.({ frames: (DETERMINISTIC ? ['✻'] : SPINNER).map(accent), intervalMs: 120 });
      }
      if (governed && state.pending_transition) {
        const parent = state.pending_transition.parent;
        const ordinary = state.workspace_mode === 'project' && (!state.goal || ['cleared', 'completed'].includes(state.goal.status));
        if (!ordinary) governed.session.sessionManager.appendCustomMessageEntry('kernelbridge-retained-facts', JSON.stringify({
          goal: state.goal, task_version: state.task_version, budget: state.budget, best_accepted: state.best,
          single_best: state.single_best, platform: state.platform, pending_effect_ids: state.pending_effect_ids,
          user_constraints: Object.values(state.inputs).filter(item => item.initiator === 'user' && item.kind !== 'control').map(item => item.text),
        }), false);
        governed.session.refreshContext();
        if (state.workspace_mode === 'project') {
          await control.call('configuration', { operation: 'context_started', pi_session_id: ctx.sessionManager.getSessionId() });
        } else await control.call('trial', { trial_id: randomUUID(), source_sha256: parent.source_sha256,
          parent_entry_id: parent.entry_id, parent_pi_session_id: parent.pi_session_id,
          pi_session_id: ctx.sessionManager.getSessionId(), transition_id: state.pending_transition.transition_id });
        await refresh();
        await snapshot('checkpoint');
      }
    });
    pi.on('input', async event => {
      explorationBoundary();
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
    pi.on('tool_result', async event => {
      consecutiveErrors = event.isError ? consecutiveErrors + 1 : 0;
      if (consecutiveErrors >= (state?.fork_policy?.minimum_observations ?? 3)) {
        await suggestFork('连续工具失败，建议重新检查假设。', 'errors-' + Math.floor(consecutiveErrors / 3));
      }
    });
    // The native loader row shows elapsed time and live children, like other
    // coding CLIs; it is presentation only and stops with the settled run.
    let workingTimer, activity;
    const stopWorking = () => { clearInterval(workingTimer); workingTimer = undefined; activity = undefined; context?.ui.setWorkingMessage?.(); };
    // The row stays on one line: details give way at the terminal's current width.
    const tick = started => context?.ui.setWorkingMessage?.(workingMessage(state, started, activity, process.stdout.columns));
    pi.on('agent_start', () => {
      runGoalId = state?.goal?.goal_id ?? null;
      userInterrupted = false;
      if (!interactive || !context?.ui.setWorkingMessage) return;
      stopWorking();
      const started = Date.now();
      activity = '思考中';
      tick(started);
      workingTimer = setInterval(() => tick(started), 1000);
      workingTimer.unref?.();
      workingTimer.started = started;
    });
    pi.on('tool_execution_start', event => {
      if (!workingTimer) return;
      activity = TOOL_ACTIVITY[event.toolName] ?? `运行 ${event.toolName}`;
      tick(workingTimer.started);
    });
    pi.on('tool_execution_end', () => {
      if (!workingTimer) return;
      activity = '思考中';
      tick(workingTimer.started);
    });
    pi.on('agent_settled', async event => {
      let finished;
      settlement = new Promise(resolve => { finished = resolve; });
      // Errors still reach Pi: an unknown effect must surface as InDoubt (input-settlement.mjs).
      try { await settleTurn(event); }
      finally { finished(); }
    });
    async function settleTurn(event) {
      stopWorking();
      const providerFailed = governed?.controller.stopResult?.status === 'providers_failed';
      const setupFailed = governed?.controller.stopResult?.status === 'seed_failed';
      const limitReached = ['budget_exhausted', 'budget_limit', 'trial_limit', 'step_limit'].includes(governed?.controller.stopResult?.status);
      const failure = !userInterrupted && !providerFailed && !setupFailed && !limitReached
        ? governed?.controller.failure : null;
      const failureSummary = failure ? Array.from(String(failure)).slice(0, 2048).join('') : undefined;
      if (failure) {
        // Record the cause before reconciling effects: an unknown effect may
        // still prevent settlement. This is never a synthetic user interrupt.
        await control.call('configuration', { operation: 'harness_failure', reason: 'harness_failure',
          summary: failureSummary, goal_id: runGoalId, input_ids: [...runningInputs] });
        showProductFeedback(context, `运行时失败：${failureSummary}`, true);
      }
      if (limitReached) showProductFeedback(context, '已达到显式设置的执行上限。当前请求已停止，未再调用模型。', true);
      if (providerFailed) {
        await effects.call('end_failed_turn', { native_checkpoint: await governed.checkpoint() });
      }
      if ((event.aborted || failure) && governed) {
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
        if (!failure && !userInterrupted) await stop();
      }
      // Claude Code leaves a visible mark where a turn stopped; otherwise the
      // transcript ends mid-thought with no explanation.
      if (event.aborted && runningInputs.size) showProductFeedback(context, '已中断。工具调用的回执已保留，可以继续输入。');
      for (const inputId of [...runningInputs]) {
        await control.call('transition', { input_id: inputId,
          status: failure ? 'failed' : event.aborted ? 'interrupted' : governed?.controller.failure || providerFailed || setupFailed || limitReached ? 'failed' : 'completed',
          ...(failure ? { reason: 'harness_failure', summary: failureSummary } : {}) });
        runningInputs.delete(inputId);
      }
      activeInput = undefined;
      if (governed) {
        try { await snapshot('checkpoint'); }
        catch (error) {
          // Another input already reached the host; its own application checkpoints this boundary.
          if (!/settled execution boundary/.test(String(error?.message ?? error))) throw error;
        }
      }
      await refresh();
      const plateau = state.fork_suggestion;
      if (plateau) await suggestFork(plateau, 'plateau-' + state.model_turns);
      if (interactive && !event.aborted && !failure && !exiting) setImmediate(() => { void advance().catch(report); });
    }
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
    pi.on('session_compact', async () => {
      if (governed) await snapshot('checkpoint');
      compactions++;
      if (compactions >= 2) await suggestFork('这个上下文已多次压缩。', 'compactions-' + compactions);
    });
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
    await settlement;
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
    try {
      await control.call('transition', { input_id: receipt.input_id, status: 'applied',
        pi_session_id: manager.getSessionId(), entry_id: entryId, ...(native_checkpoint ? { native_checkpoint } : {}) });
    } catch (error) {
      // A pause or clear can withdraw an automatic input after Pi accepted it
      // but before its model call. The following model_turn then stops without
      // calling the provider; this is not a transport failure.
      const current = (await control.state()).inputs?.[receipt.input_id];
      if (current && ['interrupted', 'failed'].includes(current.status)) return;
      throw error;
    }
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

  const failControl = error => {
    if (governed) { governed.controller.fail(new Error(error)); void governed.session.abort(); }
  };
  const recoverObservations = () => {
    if (disposed || observationFallback) return;
    clearTimeout(observationTimer);
    // Observation loss cannot restart execution. Read the original queue over
    // the independent control channel and deliver only its retained identities;
    // delivered protects a receipt already handed to Pi from a second delivery.
    observationFallback = (async () => {
      await refresh();
      if (disposed) return;
      updateSubagentProgress(state.subagents);
      for (const receipt of Object.values(state.inputs ?? {})) {
        if (receipt.initiator === 'agent' && receipt.status === 'queued') await deliver(receipt);
      }
    })().catch(error => { if (!disposed) report(error); }).finally(() => {
      observationFallback = undefined;
      if (!disposed && !control.closed) {
        observationTimer = setTimeout(recoverObservations, 1000);
        observationTimer.unref?.();
      }
    });
  };
  const unsubscribeControl = observer === control ? () => {} : control.subscribe(event => {
    if (event.type === 'disconnected') failControl(event.error);
  });
  const unsubscribe = observer.subscribe(event => {
    if (event.type === 'state') {
      acceptState(event.state);
      updateSubagentProgress(state.subagents);
      syncTools();
      context?.ui.setStatus('kernelbridge', goalSummary(state));
    } else if (event.type === 'input' && event.receipt.channel !== 'tui') {
      // prompt() resolves after a run when idle. Do not serialize later steering
      // behind that promise; Pi owns its queue and the host owns durable order.
      const delivery = deliver(event.receipt).catch(report);
      inputDelivery = Promise.all([inputDelivery, delivery]);
    } else if (event.type === 'control' && ['interrupt', 'pause'].includes(event.operation)) {
      userInterrupted = true;
      if (event.operation === 'interrupt') void governed?.session.abort();
    } else if (event.type === 'control' && ['resume', 'set'].includes(event.operation)) {
      if (interactive) setImmediate(() => { void advance().catch(report); });
    } else if (event.type === 'takeover') {
      exiting = true;
    } else if (event.type === 'disconnected') {
      if (observer === control) failControl(event.error);
      else {
        showProductFeedback(context, '实时输出连接已断开，原任务仍在执行；完成后会显示完整结果。', true);
        recoverObservations();
      }
    }
  });

  const actions = {
    extension, inputs,
    commands: () => PRODUCT_COMMANDS.filter(command => state?.workspace_mode !== 'project'
      || !['import', 'detach', 'changelog', 'login', 'logout'].includes(command.name)).map(command => ({ ...command,
        ...(state?.workspace_mode === 'project' && command.name === 'new' ? { description: '开始新对话，保留工作文件' } : {}) })),
    presentInput: text => text.startsWith('[子代理结果] ') && Object.values(state?.inputs ?? {}).some(receipt => receipt.initiator === 'agent' && receipt.text === text)
      ? '⏺ ' + text.split('\n')[0].slice('[子代理结果] '.length).replace(/（agent_id: [^）]*）/, '')
      : policyInputs.has(text)
      && !Object.values(state?.inputs ?? {}).some(receipt => receipt.initiator === 'user' && receipt.text === text)
      ? continuationLabel(text) : text,
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
      if (!state.trials.length && state.workspace_mode !== 'project') {
        await control.call('trial', { trial_id: randomUUID(), pi_session_id: manager.getSessionId(),
          source_sha256: checkpoint.source_sha256 });
        await refresh();
      }
    },
    async bind(value, options = {}) {
      governed = value;
      interactive = options.interactive === true;
      await observer.observe();
      const patch = JSON.parse(await readFile(new URL('../../../patches/pi-coding-agent-1.5.0-host-actions.json', import.meta.url), 'utf8'));
      await control.call('configuration', { operation: 'runtime_manifest', ...governed.manifest,
        command_policy_version: COMMAND_POLICY_VERSION, patch_sha256: patch.patch_sha256 });
      await refresh();
    },
    async initial() {
      await refresh();
      for (const receipt of Object.values(state.inputs).filter(item => item.status === 'queued' && item.kind === 'control')) await deliver(receipt);
      const entries = governed.session.sessionManager.getBranch();
      // The editor is live before initial() settles. A receipt submitted from
      // it in this process is already owned by Pi's input loop; recovering or
      // replaying it here would submit the same text twice.
      const recoverable = item => item.status === 'queued' && item.kind !== 'control' && !delivered.has(item.input_id);
      for (const receipt of Object.values(state.inputs).filter(recoverable)) {
        const marker = entries.findIndex(entry => entry.type === 'custom' && entry.customType === 'kernelbridge.input' && entry.data?.input_id === receipt.input_id);
        const user = marker < 0 ? undefined : entries.slice(marker + 1).find(entry => entry.type === 'message' && entry.message.role === 'user');
        if (user) {
          await persistInput(receipt, user.id);
          await control.call('transition', { input_id: receipt.input_id, status: 'interrupted' });
          runningInputs.delete(receipt.input_id);
        }
      }
      await refresh();
      const queued = Object.values(state.inputs).filter(recoverable);
      if (!queued.length && !governed.restored && !Object.values(state.inputs).some(item => item.kind !== 'control') && state.workspace_mode !== 'project') {
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
        // Pi clears isStreaming before it awaits agent_settled extensions. The
        // host may still be settling the previous input at this boundary;
        // accept the next request as a follow-up and let Pi's native input
        // queue deliver it after the previous prompt finishes.
        const settling = runningInputs.size || applying.length || persistedInputs.length
          || Object.values(state?.inputs ?? {}).some(item => item.kind !== 'control' && item.status === 'applied');
        const kind = status.streaming || status.compacting ? 'steer' : settling ? 'follow_up' : 'prompt';
        const receipt = await inputs.accept(normalized, kind);
        if (receipt.status !== 'queued') return { action: 'handled', message: 'Original input was already applied' };
        // Native submission owns this delivery before agent_settled/next can
        // observe the durable queued receipt. Restart recovery still owns any
        // receipt that was accepted but never persisted by Pi.
        delivered.add(receipt.input_id);
        return { action: 'continue', text: normalized };
      }
      if (DISABLED.has(command)) return { action: 'reject', message: `${command} is disabled by the host profile` };
      if (!actions.commands().some(item => '/' + item.name === command)) return { action: 'reject', message: `不可用的命令：${command}。输入 /help 查看支持的命令。` };
      if (command === '/help' || command === '/hotkeys') return { action: 'handled', message: commandHelp(state.workspace_mode) };
      if (command === '/latest') return { action: 'handled' };
      if (command === '/trace') {
        // The CLI process hosts the viewer; this worker has no Journal access of its own.
        if (!process.send) return { action: 'handled', message: '在另一个终端运行 kernelagent trace 打开实时轨迹。' };
        const reply = await new Promise(resolve => {
          const timer = setTimeout(() => { process.off('message', listen); resolve({ error: '轨迹服务启动超时' }); }, 10000);
          function listen(message) { if (message?.type !== 'trace_ready') return; clearTimeout(timer); process.off('message', listen); resolve(message); }
          process.on('message', listen);
          process.send({ type: 'open_trace' });
        });
        return { action: 'handled', message: reply.error ? `无法打开实时轨迹：${reply.error}`
          : `实时轨迹${reply.opened ? '已在浏览器打开' : ''}：${reply.url}\n本会话运行期间持续更新；退出界面后服务停止。` };
      }
      if (command === '/subagents' || command === '/agents') {
        const current = await refresh();
        if (current.subagent_config?.enabled === false) return { action: 'handled', message: '子代理未启用。' };
        const types = current.subagent_types?.length ? current.subagent_types : BUILTIN_AGENTS;
        if (!interactive || !context?.ui.custom) {
          return { action: 'handled', message: [...types.map(type => `${type.name} · ${type.description}`),
            ...(current.subagents ?? []).map(child => `${child.agent} · ${child.status} · ${child.summary || child.task}`)].join('\n') };
        }
        const choice = await context.ui.custom((tui, _theme, _keybindings, done) => agentsPanel(() => state.subagents ?? [],
          () => state.subagent_types?.length ? state.subagent_types : BUILTIN_AGENTS, done,
          () => tui.requestRender(), () => tui.terminal.rows));
        if (choice?.stop) {
          await effects.call('application_stop_child', { agent_id: choice.stop });
          return { action: 'handled', message: '已请求停止该子代理；它会在当前步骤结束后报告结果。' };
        }
        return { action: 'handled' };
      }
      if (command === '/goal') {
        const { operation, ...values } = parseGoalCommand(text);
        if (operation === 'status') return { action: 'handled', message: (await refresh(), () => statePresentation(state)) };
        if (operation === 'resume' && governed.controller.failure) return { action: 'handled',
          message: '当前运行时已失败。先 /quit，重新连接并排除原错误后再显式 /goal resume。若历史消息无效，需依据可信回执或有效检查点修复；重新连接不会修复历史，也不会自动重跑原效果。' };
        await control.control(operation, { channel: 'tui', ...values,
          ...(acceptedCommand ? { input_id: acceptedCommand.input_id + '-goal' } : {}) });
        return { action: 'handled', message: (await refresh(), () => statePresentation(state)) };
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
        return { action: 'handled', message: (await refresh(), () => statePresentation(state)) };
      }
      if (command === '/name' && arguments_.length) {
        await control.call('configuration', { operation: 'rename', name: arguments_.join(' ').slice(0, 120) });
        return { action: 'continue' };
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
        if (!governed.session.sessionManager.getBranch().some(item => item.type === 'message' && ['user', 'assistant'].includes(item.message.role))) {
          return { action: 'handled', message: '当前对话还没有消息可分支。' };
        }
        await snapshot('checkpoint');
        const entry = governed.session.sessionManager.getLeafId();
        if (!entry) return { action: 'handled', message: '当前对话还没有消息可分支。' };
        const result = await governed.runtime.fork(entry, { position: 'at' });
        if (result.cancelled) throw new Error('Fork was refused; the original session is retained.');
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
      if (command === '/fork') {
        if (!arguments_[0] && !governed.session.sessionManager.getBranch().some(item => item.type === 'message' && ['user', 'assistant'].includes(item.message.role))) {
          return { action: 'handled', message: '当前对话还没有消息可分支。' };
        }
        await snapshot('checkpoint');
        const entry = arguments_[0] ?? governed.session.sessionManager.getLeafId();
        if (!entry) return { action: 'handled', message: '当前对话还没有消息可分支。' };
        if (!governed.session.sessionManager.getEntry(entry)) throw new Error('Unknown fork entry; choose a retained entry ID.');
        const result = await governed.runtime.fork(entry, { position: 'at' });
        if (result.cancelled) throw new Error('Fork was refused; the original session is retained.');
        return { action: 'handled' };
      }
      if (command === '/resume' && state.workspace_mode === 'project') {
        const all = arguments_.includes('--all');
        const query = arguments_.filter(value => value !== '--all').join(' ');
        const available = (await sessions({ registry: dirname(control.directory) })).filter(item => item.workspace_mode === 'project'
          && (item.has_history || item.application_session_id === control.applicationSessionId || query));
        let selected;
        if (query) {
          const scoped = available.filter(item => all || item.cwd === state.cwd);
          const exact = scoped.filter(item => item.application_session_id === query || item.title === query);
          const matches = exact.length ? exact : scoped.filter(item => item.application_session_id.startsWith(query) || item.title?.includes(query));
          if (matches.length !== 1) return { action: 'handled', message: matches.length ? '匹配到多个会话，请用完整 ID。' : '没有匹配的会话；用 /resume --all 查看其他项目。' };
          selected = matches[0];
        } else {
          if (!available.length) return { action: 'handled', message: '当前项目没有历史会话；用 /resume --all 查看其他项目。' };
          const project = await projectInfo(state.cwd);
          const choice = await context.ui.custom((tui, _theme, _keybindings, done) => sessionPicker(available, done, () => tui.requestRender(),
            { cwd: state.cwd, all, project, currentId: control.applicationSessionId, rows: () => tui.terminal.rows }));
          if (!choice) return { action: 'handled' };
          selected = available.find(item => item.application_session_id === choice);
        }
        if (selected.application_session_id === control.applicationSessionId) return { action: 'handled', message: '已经在这个会话中。' };
        if (!process.send) return { action: 'handled', message: `请运行 kernelagent resume ${selected.application_session_id}` };
        await snapshot('checkpoint');
        await new Promise((resolve, reject) => process.send({ type: 'resume_application', identity: selected.application_session_id }, error => error ? reject(error) : resolve()));
        return { action: 'continue', text: '/quit' };
      }
      if (command === '/resume' || command === '/import') {
        const selected = await control.call('select_session', { path: arguments_[0] });
        if (selected.sessions) {
          const path = await context.ui.select('恢复原会话', selected.sessions);
          if (!path) return { action: 'handled' };
          Object.assign(selected, await control.call('select_session', { path }));
        }
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
    interrupt: async () => { userInterrupted = true; await stop(); },
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
    dispose() { disposed = true; clearTimeout(observationTimer); unsubscribe(); unsubscribeControl(); },
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
