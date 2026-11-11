import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { codegenSettings } from './codegen.mjs';
import { parseGoalCommand } from '../harness/pi/command-policy.mjs';
import { loadInteractiveMode } from '../harness/pi/interactive-patch.mjs';
import { installProductUI, showProductFeedback } from '../harness/pi/product-ui.mjs';

const commands = [
  { name: 'help', description: '命令与快捷键' }, { name: 'config', description: '加载模型配置', argumentHint: 'PATH' },
  { name: 'model', description: '查看或选择模型', argumentHint: '[MODEL]' },
  { name: 'settings', description: '当前目录和模型设置' }, { name: 'resume', description: '恢复当前项目最近会话' },
  { name: 'goal', description: '显式开启持续目标', argumentHint: '目标内容' }, { name: 'quit', description: '退出' },
];
const help = '输入任务后开始执行；完成后等待下一条输入。\n/config PATH：加载配置 · /model MODEL：选择模型 · /resume：恢复会话\n/goal 目标内容：显式开启持续执行 · /quit：退出\nEnter：发送 · Shift+Enter：换行 · Ctrl+C：清空，再按退出 · Ctrl+D：空输入时退出';

/** Configured sessions open the real governed Pi UI immediately, with no prompt. */
export async function startScreen(options) {
  if (!process.stdin.isTTY || !process.stdout.isTTY) throw new Error('交互模式需要终端；脚本调用请使用 run 或 SDK。');
  let settings = await codegenSettings(options);
  if (!settings.problem) return { settings, submission: { prompt: options.prompt, goal: options.goal } };
  if (options.prompt || options.goal) throw new Error(settings.problem);
  // Model setup also uses InteractiveMode. Its in-memory session cannot execute.
  const directory = await mkdtemp(join(tmpdir(), 'kernelbridge-setup-'));
  let runtime, mode, context, finish;
  const result = new Promise(resolve => { finish = resolve; });
  const state = () => ({ workspace_mode: 'project', cwd: settings.cwd, model: settings.model,
    inputs: {}, trials: [], budget: { limit: settings.credits, spent: 0, reserved: 0 } });
  const feedback = (message, error = false) => showProductFeedback(context, message, error);
  const actions = {
    commands: () => commands, feedback, initial: () => undefined, presentInput: text => text,
    interrupt: async () => {}, resumeCommand: () => undefined, exit: async () => true,
    async submit(raw) {
      const text = raw.trim(); if (!text) return { action: 'handled' };
      const [command, ...args] = text.split(/\s+/), value = args.join(' ');
      if (['/quit', '/exit'].includes(command)) { finish(null); return { action: 'handled' }; }
      if (command === '/resume') { finish({ resume: value || true }); return { action: 'handled' }; }
      if (command === '/help') return { action: 'handled', message: help };
      if (command === '/settings') return { action: 'handled', message: `配置：${settings.path}\n${settings.problem}` };
      if (command === '/config' || command === '/model') {
        if (!value) return { action: 'handled', message: command === '/config' ? `配置：${settings.path}` : `模型：${settings.model ?? '未配置'}` };
        const next = { ...options, [command === '/config' ? 'config' : 'model']: value };
        settings = await codegenSettings(next); Object.assign(options, next);
        if (!settings.problem) finish({ settings, submission: {} });
        return { action: 'handled', message: settings.problem ?? `模型：${settings.model}` };
      }
      if (command === '/goal') {
        const goal = parseGoalCommand(text);
        if (goal.operation !== 'set') return { action: 'handled', message: '尚未设置 Goal。使用 /goal 目标内容 开始。' };
      } else if (text.startsWith('/')) return { action: 'reject', message: `不可用的命令：${command}。输入 /help 查看支持的命令。` };
      return { action: 'reject', message: settings.problem };
    },
  };
  try {
    const sdk = await import('@earendil-works/pi-coding-agent');
    const { InMemoryCredentialStore } = await import('@earendil-works/pi-ai');
    runtime = await sdk.createAgentSessionRuntime(async options => {
      const modelRuntime = await sdk.ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null,
        modelsStorePath: join(directory, 'models-cache.json'), allowModelNetwork: false, refreshOnCreate: false });
      const settingsManager = sdk.SettingsManager.inMemory({ quietStartup: 'header', retry: { enabled: false }, compaction: { enabled: false } });
      const resourceLoader = new sdk.DefaultResourceLoader({ ...options, settingsManager,
        noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
        agentsFilesOverride: () => ({ agentsFiles: [] }),
        extensionFactories: [pi => {
          pi.on('session_start', (_event, ctx) => { context = ctx; installProductUI(ctx, state); feedback(settings.problem); });
          pi.on('session_shutdown', () => rm(directory, { recursive: true, force: true }));
          pi.on('input', () => ({ action: 'handled' }));
          pi.on('user_bash', () => ({ result: { output: '请先配置模型并进入工作会话。', exitCode: 1, cancelled: false, truncated: false } }));
        }] });
      await resourceLoader.reload();
      const services = { cwd: options.cwd, agentDir: options.agentDir, modelRuntime, settingsManager, resourceLoader, diagnostics: [] };
      const created = await sdk.createAgentSession({ ...options, ...services, noTools: 'all', tools: [] });
      created.session.agent.streamFunction = () => { throw new Error('Setup session cannot execute model requests'); };
      return { ...created, services, diagnostics: [] };
    }, { cwd: settings.cwd, agentDir: directory, sessionManager: sdk.SessionManager.inMemory(settings.cwd) });
    const { InteractiveMode } = await loadInteractiveMode();
    mode = new InteractiveMode(runtime, { hostActions: actions });
    await mode.init();
    return await result;
  } finally {
    mode?.stop();
    await runtime?.dispose();
    await rm(directory, { recursive: true, force: true });
  }
}
