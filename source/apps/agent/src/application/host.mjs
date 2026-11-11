import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, readFile, writeFile, open, readdir, stat } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createReadStream } from 'node:fs';
import { createInterface } from 'node:readline';
import { ApplicationClient } from './client.mjs';
import { loadCodegenCredentials } from './credentials.mjs';

const agentRoot = fileURLToPath(new URL('../../', import.meta.url));
const bundleRoot = resolve(agentRoot, '../../..');
const registryRoot = () => process.env.KERNELBRIDGE_SESSIONS ?? join(homedir(), '.kernelbridge', 'sessions');

async function save(path, value) {
  const file = await open(path, 'wx', 0o600);
  try { await file.writeFile(JSON.stringify(value, null, 2) + '\n'); await file.sync(); }
  finally { await file.close(); }
  const directory = await open(dirname(path), 'r');
  try { await directory.sync(); } finally { await directory.close(); }
}

export async function applicationDirectory(identity) {
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(identity)) throw new Error('Invalid application session ID');
  return join(registryRoot(), identity);
}

async function offlineHistory(path, identity, fallback) {
  // Discovery is advisory. The host separately verifies the complete journal
  // before resuming execution; an incomplete last line is not a new title.
  const stream = createReadStream(path, { encoding: 'utf8' });
  const lines = createInterface({ input: stream, crlfDelay: Infinity });
  let name, firstPrompt, goal;
  try {
    for await (const line of lines) {
      if (!line.includes('application/')) continue;
      let row;
      try { row = JSON.parse(line); } catch { continue; }
      if (row.application_session_id !== identity) continue;
      if (row.kind === 'application/configuration' && row.data?.operation === 'rename') name = row.data.name;
      if (row.kind === 'application/goal') goal = row.data?.objective;
      if (row.kind === 'application/input_accepted' && row.data?.kind === 'prompt') firstPrompt ??= row.data.text;
    }
  } catch { /* Missing/unreadable history remains discoverable by its ID. */ }
  finally { lines.close(); stream.destroy(); }
  return { title: (name ?? goal ?? firstPrompt ?? fallback).slice(0, 120), has_history: Boolean(name || goal || firstPrompt) };
}

export async function sessions({ registry = registryRoot() } = {}) {
  let names;
  // A running Pi worker has an isolated HOME. Callers in that scope must use
  // the registry of their authenticated host, not infer it from process.env.
  registry = resolve(registry);
  try { names = await readdir(registry); } catch (error) { if (error.code === 'ENOENT') return []; throw error; }
  const result = [];
  for (const name of names.sort().filter(name => name.startsWith('application-'))) {
    let summary, journal;
    try {
      const descriptor = JSON.parse(await readFile(join(registry, name, 'descriptor.json'), 'utf8'));
      const config = JSON.parse(await readFile(descriptor.config, 'utf8'));
      const task = JSON.parse(await readFile(descriptor.task, 'utf8'));
      journal = join(config.state_dir, 'events.jsonl');
      summary = { application_session_id: name, output: descriptor.output, cwd: config.workspace,
        workspace_mode: config.workspace_mode ?? 'isolated', title: task.goal.slice(0, 120),
        updated: (await stat(join(config.state_dir, 'events.jsonl')).catch(() => ({ mtimeMs: 0 }))).mtimeMs };
      const client = await ApplicationClient.connect(descriptor.host_directory, { timeoutMs: 1000 });
      try {
        const state = await client.state();
        const firstPrompt = Object.values(state.inputs ?? {}).find(item => item.kind === 'prompt')?.text;
        result.push({ ...summary, title: (state.name ?? state.goal?.objective ?? firstPrompt ?? summary.title).slice(0, 120),
          has_history: Boolean(state.name || state.goal || firstPrompt),
          goal: state.goal, lease: state.lease, connected: true });
      } finally { client.close(); }
    } catch (error) {
      if (summary && journal) Object.assign(summary, await offlineHistory(journal, name, summary.title));
      result.push({ ...summary, application_session_id: name, connected: false, error: error.message });
    }
  }
  return result.sort((a, b) => (b.updated ?? 0) - (a.updated ?? 0));
}

/** Starts the detached trusted executor, then returns only a credential-free socket client. */
export async function startApplication(options) {
  const identity = options.applicationSessionId ?? `application-${randomUUID()}`;
  const directory = await applicationDirectory(identity);
  await mkdir(directory, { mode: 0o700, recursive: true });
  const output = resolve(options.output);
  const taskPath = resolve(options.task);
  const task = JSON.parse(await readFile(taskPath, 'utf8'));
  const configPath = resolve(options.config);
  const config = JSON.parse(await readFile(configPath, 'utf8'));
  const defaults = { max_trials: options.maxTrials ?? null,
    max_model_turns: options.maxModelTurns ?? null,
    token_budget: options.goalTokenBudget ?? null,
    max_forks: options.maxForks ?? null,
    fork_policy: options.forkPolicy ?? { enabled: true, minimum_observations: 3, cooldown_turns: 3 } };
  for (const key of ['max_trials', 'max_model_turns', 'max_forks', 'token_budget']) {
    if (defaults[key] !== null && (!Number.isSafeInteger(defaults[key]) || defaults[key] < (key === 'max_forks' ? 0 : 1))) throw new Error(`Invalid Goal ${key}`);
  }
  const forkPolicy = defaults.fork_policy;
  if (typeof forkPolicy.enabled !== 'boolean' || !Number.isSafeInteger(forkPolicy.minimum_observations) || forkPolicy.minimum_observations < 2
      || !Number.isSafeInteger(forkPolicy.cooldown_turns) || forkPolicy.cooldown_turns < 1) throw new Error('Invalid automatic fork policy');
  if (!['pause', 'continue'].includes(options.onDetach ?? 'pause')) throw new Error('Invalid on-detach policy');
  const goal = options.sessionOnly !== false ? null : {
    goal_id: `goal-${randomUUID()}`, objective: task.goal, status: 'active', task_version: 1,
    on_detach: options.onDetach ?? 'pause', ...defaults,
  };
  const descriptor = { schema_version: 'kernelbridge.application-host/v1', application_session_id: identity,
    host_directory: directory, output, task: taskPath, config: configPath, goal, goal_defaults: defaults,
    python: options.python, expected_python_modules: options.expectedPythonModules,
    node: options.node ?? process.execPath, worker_entry: join(agentRoot, 'src/application/worker.mjs'),
    task_sha256: createHash('sha256').update(await readFile(taskPath)).digest('hex'),
    config_sha256: createHash('sha256').update(await readFile(configPath)).digest('hex') };
  await save(join(directory, 'descriptor.json'), descriptor);
  await mkdir(join(output, 'logs'), { recursive: true, mode: 0o700 });
  await save(join(output, 'application.json'), { application_session_id: identity, host_directory: directory });
  return launchApplication(descriptor, options.environment);
}

export async function launchApplication(descriptor, environment = {}) {
  const configBytes = await readFile(descriptor.config);
  if (createHash('sha256').update(configBytes).digest('hex') !== descriptor.config_sha256) throw new Error('Frozen session configuration changed');
  const config = JSON.parse(configBytes.toString('utf8'));
  if (config.workspace_mode === 'project') await loadCodegenCredentials(config.providers);
  const logfile = await open(join(descriptor.output, 'logs/host.log'), 'a', 0o600);
  let child;
  try {
    child = spawn(descriptor.python, ['-B', '-P', join(agentRoot, 'python/generated_startup.py'),
      '--expected-python-modules', descriptor.expected_python_modules, 'application-host',
      '--descriptor', join(descriptor.host_directory, 'descriptor.json')], {
      detached: true, stdio: ['ignore', logfile.fd, logfile.fd],
      env: { ...process.env, ...environment, PYTHONDONTWRITEBYTECODE: '1',
        KERNELCAT_ACTIVE_AGENT_ROOT: agentRoot, KERNELCAT_ACTIVE_NODE: descriptor.node },
    });
  } finally { await logfile.close(); }
  let failure;
  child.once('error', error => { failure = error; });
  const exited = new Promise(resolveExit => child.once('exit', code => resolveExit(code)));
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    if (failure) throw failure;
    try { const client = await ApplicationClient.connect(descriptor.host_directory); child.unref(); return client; }
    catch (error) {
      if (!['ENOENT', 'ECONNREFUSED'].includes(error.code)) throw error;
    }
    const code = await Promise.race([exited, new Promise(resolveWait => setTimeout(() => resolveWait(undefined), 100))]);
    if (code !== undefined) throw new Error(`Application host exited (${code}); inspect ${join(descriptor.output, 'logs/host.log')}`);
  }
  child.unref();
  throw new Error('Host startup acknowledgement timed out; inspect the original host before restarting');
}

export async function attachApplication(identity, { restart = false } = {}) {
  const directory = await applicationDirectory(identity);
  if (!restart) return ApplicationClient.connect(directory);
  const descriptor = JSON.parse(await readFile(join(directory, 'descriptor.json'), 'utf8'));
  try { return await ApplicationClient.connect(directory); }
  catch (error) {
    if (!['ENOENT', 'ECONNREFUSED'].includes(error.code)) throw error;
    return launchApplication(descriptor);
  }
}

export async function prepareApplication({ task, profile, output, sessionOnly = true, onDetach }) {
  const expand = value => typeof value === 'string' ? value.replaceAll('{bundle}', bundleRoot).replaceAll('{run}', resolve(output))
    : Array.isArray(value) ? value.map(expand) : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).map(([key, item]) => [key, expand(item)])) : value;
  const selected = expand(JSON.parse(await readFile(profile, 'utf8')));
  const python = selected.python;
  if (!python) throw new Error('Profile must select an installed Python executable');
  // Task/profile preparation validates and freezes evaluator inputs without dispatch.
  const result = await new Promise((accept, reject) => {
    const child = spawn(python, ['-B', join(bundleRoot, 'examples/cann/prepare.py'), '--task', resolve(task),
      '--profile', resolve(profile), '--output', resolve(output), '--application-host', ...(sessionOnly ? ['--session-only'] : [])], { stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-16384); });
    child.once('error', reject);
    child.once('exit', code => code === 0 ? accept(JSON.parse(stdout)) : reject(new Error(stderr || `Preparation exited ${code}`)));
  });
  return startApplication({ task: join(output, 'task.json'), config: join(output, 'config.json'), output,
    python: selected.python, node: selected.node, expectedPythonModules: selected.expected_python_modules,
    environment: { PYTHONPATH: result.pythonPath }, sessionOnly,
    maxTrials: selected.goal?.max_trials, maxModelTurns: selected.application?.max_model_turns,
    goalTokenBudget: selected.application?.token_budget,
    maxForks: selected.application?.max_forks, forkPolicy: selected.application?.fork_policy,
    onDetach: onDetach ?? selected.application?.on_detach });
}
