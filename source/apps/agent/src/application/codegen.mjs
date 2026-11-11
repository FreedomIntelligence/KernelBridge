import { mkdir, readFile, realpath, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { startApplication, attachApplication } from './host.mjs';
import { loadCodegenCredentials } from './credentials.mjs';

const agentRoot = fileURLToPath(new URL('../../', import.meta.url));
const home = () => process.env.KERNELBRIDGE_HOME ?? join(homedir(), '.kernelbridge');
const projectKey = cwd => createHash('sha256').update(cwd).digest('hex').slice(0, 24);
const save = (path, value) => writeFile(path, JSON.stringify(value, null, 2) + '\n', { mode: 0o600, flag: 'wx' });

export async function codegenSettings(options) {
  const cwd = await realpath(resolve(options.cwd ?? process.cwd()));
  const path = options.config ?? process.env.KERNELBRIDGE_CONFIG ?? join(home(), 'config.json');
  let config = {};
  try { config = JSON.parse(await readFile(path, 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT' || options.config || process.env.KERNELBRIDGE_CONFIG) throw error; }
  if (!config || typeof config !== 'object' || Array.isArray(config)) throw new Error('模型配置必须是 JSON 对象。');
  if (config.rsi || config.evaluator || config.business_tools?.length) {
    throw new Error('验收配置需要显式的 --task 和 --profile（或 --config）；普通 codegen 请使用仅包含模型设置的配置。');
  }
  const providers = structuredClone(config.providers ?? {});
  let routes = [...(config.policy?.routes ?? ['primary'])];
  if (typeof providers !== 'object' || Array.isArray(providers) || !Array.isArray(routes) || !routes.length
      || routes.some(route => typeof route !== 'string' || !route.trim()) || new Set(routes).size !== routes.length) {
    throw new Error('providers 必须是模型路由对象，policy.routes 必须是不重复的非空路由列表。');
  }
  if (options.model && Object.hasOwn(providers, options.model) && !options.provider) {
    routes = [options.model, ...routes.filter(route => route !== options.model)];
  } else if (options.model) {
    const provider = options.provider ?? providers[routes[0]]?.provider ?? 'openai';
    const previous = options.provider && options.provider !== providers[routes[0]]?.provider ? {} : providers[routes[0]];
    providers[routes[0]] = { provider,
      base_url: provider === 'anthropic' ? 'https://api.anthropic.com' : 'https://api.openai.com/v1',
      api_key_env: provider === 'anthropic' ? 'ANTHROPIC_API_KEY' : 'OPENAI_API_KEY',
      ...previous, provider, model: options.model };
  }
  for (const provider of Object.values(providers)) {
    if (!provider || typeof provider !== 'object' || Array.isArray(provider)) throw new Error('每个模型路由必须是配置对象。');
    provider.wire_options = { ...provider.wire_options, native_turns: true, tool_choice: 'auto' };
    provider.api_key_env ??= provider.provider === 'anthropic' ? 'ANTHROPIC_API_KEY' : 'OPENAI_API_KEY';
    provider.base_url ??= provider.provider === 'anthropic' ? 'https://api.anthropic.com' : 'https://api.openai.com/v1';
  }
  await loadCodegenCredentials(providers);
  const credits = options.budget === undefined ? config.credits ?? null : Number(options.budget);
  if (credits !== null && (!Number.isSafeInteger(credits) || credits <= 0)) throw new Error('--budget 必须是正整数；省略即不设置额度。');
  const problem = routes.some(route => typeof providers[route]?.model !== 'string' || !providers[route].model.trim()
    || /^(SELECT_|REPLACE_)/.test(providers[route].model)) ? '尚未配置模型。使用 /config 配置文件路径，或启动时指定 --model MODEL。'
    : routes.map(route => providers[route].api_key_env ?? (providers[route].provider === 'anthropic' ? 'ANTHROPIC_API_KEY' : 'OPENAI_API_KEY'))
      .find(name => !process.env[name]?.trim());
  return { cwd, path: resolve(path), config, providers, routes, credits,
    model: providers[routes[0]]?.model, problem: problem && (problem.startsWith('尚未') ? problem : `缺少环境变量 ${problem}；设置后重新启动，密钥不会写入会话。`) };
}

export async function resumeCodegen(options) {
  const cwd = await realpath(resolve(options.cwd ?? process.cwd()));
  let identity = options.resume;
  if (!identity || identity === true) {
    try { identity = JSON.parse(await readFile(join(home(), 'projects', projectKey(cwd) + '.json'), 'utf8')).application_session_id; }
    catch (error) { if (error.code === 'ENOENT') throw new Error('当前项目没有可恢复的会话。先输入任务创建会话。'); throw error; }
  }
  return attachApplication(identity, { restart: true });
}

/** A passive host/session may start immediately; execution requires explicit input. */
export async function startCodegen(options, settings, submission) {
  if (settings.problem) throw new Error(settings.problem);
  if (!options.python || !options.expectedPythonModules) throw new Error('请先安装运行环境；参见 README 的安装或源码开发入口。');
  const output = resolve(options.output ?? join(home(), 'runs', randomUUID()));
  await mkdir(output, { recursive: true, mode: 0o700 });
  const modules = JSON.parse(await readFile(options.expectedPythonModules, 'utf8'));
  const task = { id: 'codegen-' + randomUUID(), scope: 'codegen', goal: submission.prompt || submission.goal || 'Work on the user’s explicit coding requests in this project.',
    contract: {}, seed_files: {} };
  const config = { python_modules: modules, workspace_mode: 'project', workspace: settings.cwd,
    state_dir: join(output, 'state'), credits: settings.credits,
    policy: { context_bytes: 2097152, ...settings.config.policy, routes: settings.routes }, providers: settings.providers,
    readonly_paths: [], pi_worker: { node: process.execPath, root: agentRoot, timeout: 86400 } };
  await save(join(output, 'task.json'), task);
  await save(join(output, 'config.json'), config);
  const client = await startApplication({ ...options, config: join(output, 'config.json'), task: join(output, 'task.json'),
    output, sessionOnly: true, forkPolicy: { enabled: false, minimum_observations: 3, cooldown_turns: 3 } });
  try {
    if (submission.goal) await client.control('set', { objective: submission.goal, channel: 'cli' });
    if (submission.prompt || submission.goal) await client.input(submission.prompt || submission.goal, { channel: 'cli' });
    const projects = join(home(), 'projects');
    await mkdir(projects, { recursive: true, mode: 0o700 });
    // A small last-session pointer; it never replaces any session or receipt.
    await writeFile(join(projects, projectKey(settings.cwd) + '.json'), JSON.stringify({
      application_session_id: client.applicationSessionId, cwd: settings.cwd, output }) + '\n', { mode: 0o600 });
    return client;
  } catch (error) { client.close(); throw error; }
}
