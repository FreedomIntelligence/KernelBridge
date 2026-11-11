import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';
import { attachApplication, startApplication, prepareApplication, sessions } from './host.mjs';
import { startCodegen, resumeCodegen } from './codegen.mjs';
import { startScreen } from './start-screen.mjs';

export const HELP = `KernelBridge
  kernelagent                              Open codegen in the current project; wait for input
  kernelagent "TASK"                        Execute an initial prompt, then stay interactive
  kernelagent [-C DIR] [--config PATH] [--model MODEL] [--budget CREDITS]
  kernelagent resume [ID_OR_TITLE]          Pick a saved session in this project; --all searches all projects
  kernelagent resume --last                Resume this project's latest session
  kernelagent --continue                   Resume current project's latest session
  kernelagent --goal "OBJECTIVE"            Explicitly enable a persistent Goal
  kernelagent --goal "OBJECTIVE" --goal-token-budget TOKENS

No budget or step limit is imposed unless configured. Settings: ~/.kernelbridge/config.json.
Ordinary codegen edits the selected project directly. Goal, Trial and device acceptance are opt-in.

Explicit task / experiment mode:
  kernelagent tui --task ABS --profile ABS --output ABS [--goal-from-task] [--on-detach pause|continue]
  kernelagent tui --task ABS --config ABS --output ABS --python ABS --expected-python-modules ABS
  kernelagent tui --attach APPLICATION_ID [--restart]
  kernelagent sessions
  kernelagent session status APPLICATION_ID
  kernelagent session input APPLICATION_ID TEXT [--kind prompt|steer|follow_up] [--input-id ID]
  kernelagent session pause|resume|interrupt APPLICATION_ID
  kernelagent run --help                   Headless execution and managed-runtime commands
`;

export async function applicationMain(args) {
  if (args.includes('--help') || args.includes('-h')) { process.stdout.write(HELP); return 0; }
  if (args[0] === 'sessions') { process.stdout.write(JSON.stringify(await sessions(), null, 2) + '\n'); return 0; }
  if (args[0] === 'session') {
    const [, command, identity, text] = args;
    const client = await attachApplication(identity);
    try {
      const option = name => args.includes(name) ? args[args.indexOf(name) + 1] : undefined;
      const result = command === 'status' ? await client.state()
        : command === 'input' ? await client.input(text, { kind: option('--kind') ?? 'prompt', inputId: option('--input-id'), channel: 'cli' })
        : ['pause', 'resume', 'interrupt'].includes(command) ? await client.control(command, { channel: 'cli' })
        : (() => { throw new Error('Unknown session operation'); })();
      process.stdout.write(JSON.stringify(result, null, 2) + '\n');
      return 0;
    } finally { client.close(); }
  }
  const options = {};
  const flags = { '--task': 'task', '--config': 'config', '--profile': 'profile', '--output': 'output',
    '--python': 'python', '--expected-python-modules': 'expectedPythonModules', '--attach': 'attach', '--on-detach': 'onDetach',
    '--cwd': 'cwd', '-C': 'cwd', '--model': 'model', '--provider': 'provider', '--budget': 'budget', '--prompt': 'prompt', '--goal': 'goal',
    '--goal-token-budget': 'goalTokenBudget' };
  for (let i = ['tui', 'resume'].includes(args[0]) ? 1 : 0; i < args.length; i++) {
    if (args[i] === '--session-only') options.sessionOnly = true;
    else if (args[i] === '--goal-from-task') options.sessionOnly = false;
    else if (args[i] === '--continue' || args[i] === '-c' || args[i] === '--last') { options.resume = true; options.latest = true; }
    else if (args[i] === '--all') options.all = true;
    else if (args[i] === '--restart') options.restart = true;
    else if (!args[i].startsWith('-')) {
      const key = args[0] === 'resume' ? 'resume' : 'prompt';
      if (options[key] !== undefined) throw new Error('请把任务或会话 ID 写成一个参数。');
      options[key] = args[i];
    }
    else {
      const key = flags[args[i]];
      if (!key || options[key] !== undefined || !args[i + 1]) throw new Error(`Unknown, repeated or missing option: ${args[i]}`);
      options[key] = args[++i];
    }
  }
  if (args[0] === 'resume') options.resume ??= true;
  if (options.goalTokenBudget !== undefined) {
    options.goalTokenBudget = Number(options.goalTokenBudget);
    if (!Number.isSafeInteger(options.goalTokenBudget) || options.goalTokenBudget < 1) throw new Error('--goal-token-budget must be a positive integer');
    if (!options.goal) throw new Error('--goal-token-budget requires an explicit --goal');
  }
  if ([options.attach, options.resume, options.task].filter(Boolean).length > 1) throw new Error('Choose one of --attach, resume/--continue, or --task');
  if ((options.attach || options.resume || options.task) && (options.prompt || options.goal || options.budget || options.model)) {
    throw new Error('恢复和显式 task 使用原配置；请进入会话后输入任务或 /goal，task 预算与模型请在其配置中设置。');
  }
  if (options.provider && !['openai', 'anthropic'].includes(options.provider)) throw new Error('--provider must be openai or anthropic');
  if (options.provider && !options.model) throw new Error('--provider requires --model');
  if (options.profile && !options.task) throw new Error('--profile requires an explicit --task');
  if (options.task && (!options.output || !(options.config || options.profile))) throw new Error('--task requires --output and --config or --profile');
  if (options.goal && options.prompt) throw new Error('Choose an initial prompt or --goal, not both');
  if (options.onDetach && !['pause', 'continue'].includes(options.onDetach)) throw new Error('--on-detach must be pause or continue');
  let client;
  if (options.attach) client = await attachApplication(options.attach, { restart: options.restart });
  else if (options.resume) client = await resumeCodegen(options);
  else if (options.task) {
    options.sessionOnly ??= true;
    client = options.profile ? await prepareApplication(options) : await startApplication(options);
  } else {
    const start = await startScreen(options);
    if (!start) return 0;
    client = start.resume ? await resumeCodegen({ ...options, resume: start.resume })
      : await startCodegen(options, start.settings, start.submission);
  }
  if (!client) return 0;
  try {
    while (true) {
    if (options.onDetach) await client.control('on-detach', { value: options.onDetach, channel: 'cli' });
    await client.call('takeover');
    const deadline = Date.now() + 30000;
    while ((await client.state()).lease) {
      if (Date.now() >= deadline) throw new Error('Headless worker is still settling its original operation; attach again at the safe boundary');
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    const { ticket } = await client.call('ticket', { mode: 'interactive' });
    // The front-end inherits the TTY directly and receives no model/device secrets.
    let resumeIdentity;
    const child = spawn(process.execPath, [fileURLToPath(new URL('./worker.mjs', import.meta.url)),
      '--host', client.directory, '--ticket', ticket], {
      stdio: ['inherit', 'inherit', 'inherit', 'ipc'], env: Object.fromEntries(Object.entries({
        PATH: process.env.PATH ?? '/usr/bin:/bin', LANG: process.env.LANG ?? 'en_US.UTF-8', TERM: process.env.TERM ?? 'xterm-256color',
        KERNELBRIDGE_SESSIONS: dirname(client.directory), KERNELBRIDGE_HOME: process.env.KERNELBRIDGE_HOME,
        COLORTERM: process.env.COLORTERM, TERM_PROGRAM: process.env.TERM_PROGRAM, PI_OFFLINE: '1',
        KERNELBRIDGE_DEMO: process.env.KERNELBRIDGE_DEMO === '1' ? '1' : undefined,
      }).filter(([, value]) => value !== undefined)),
    });
    child.on('message', message => { if (message?.type === 'resume_application' && typeof message.identity === 'string') resumeIdentity = message.identity; });
    const code = await new Promise((accept, reject) => {
      child.once('error', reject);
      child.once('exit', code => accept(code ?? 1));
    });
    if (!resumeIdentity || code !== 0) return code;
    client.close();
    process.stdout.write('\n正在恢复会话…\n');
    client = await attachApplication(resumeIdentity, { restart: true });
    }
  } finally { client.close(); }
}
