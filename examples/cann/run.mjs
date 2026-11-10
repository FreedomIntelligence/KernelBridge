/** Compile authoring task -> frozen task -> existing Pi SDK. No second model loop. */
import { spawnSync } from 'node:child_process';
import { readFile, writeFile } from 'node:fs/promises';
import { openSync, closeSync, writeSync } from 'node:fs';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const bundle = resolve(here, '../..');
const HELP = 'node examples/cann/run.mjs --task ABS --profile ABS --output NEW_ABS [--mode generate|optimize] [--prepare-only]\n';

export async function prepareCannTask({ task, profile, output, mode = 'optimize', sessionSeed }) {
  for (const value of [task, profile, output]) if (!isAbsolute(value)) throw new Error('task/profile/output must be absolute');
  const data = JSON.parse(await readFile(profile, 'utf8'));
  const python = data.python.replaceAll('{bundle}', bundle);
  const run = spawnSync(python, ['-B', join(here, 'prepare.py'), '--task', task, '--profile', profile,
    '--output', output, '--mode', mode, ...(sessionSeed ? ['--session-seed', sessionSeed] : [])], { encoding: 'utf8', timeout: 45000, maxBuffer: 8 * 1024 * 1024 });
  if (run.error || run.status !== 0) throw new Error(run.error?.message || run.stderr || 'Task preparation failed');
  return JSON.parse(run.stdout);
}

export async function runCannTask(options) {
  const prepared = await prepareCannTask(options);
  if (options.prepareOnly) return { ...prepared, status: 'prepared', model_started: false, device_started: false };
  return executePrepared(prepared, options);
}

export async function executePrepared(prepared, options = {}) {
  const output = prepared.output;
  const config = JSON.parse(await readFile(join(output, 'config.json'), 'utf8'));
  const keys = Object.values(config.providers).map(x => process.env[x.api_key_env]).filter(Boolean);
  const encode = value => keys.reduce((s, key) => s.split(key).join('[REDACTED]'), JSON.stringify(value, null, 2)) + '\n';
  const save = (name, value) => writeFile(join(output, name), encode(value), { flag: 'wx', mode: 0o600 });
  const oldPythonPath = process.env.PYTHONPATH;
  process.env.PYTHONPATH = [prepared.pythonPath, oldPythonPath].filter(Boolean).join(process.platform === 'win32' ? ';' : ':');
  process.env.PYTHONDONTWRITEBYTECODE = '1';
  let events;
  let primaryError;
  try {
    options.signal?.throwIfAborted();
    if (!process.env[config.providers.primary.api_key_env]) throw new Error(`Set ${config.providers.primary.api_key_env}; preparation retained at ${output}`);
    if (prepared.localRsi) {
      const runtime = spawnSync(config.rsi.adapter_argv[0], [join(here, 'rsi_local.py'), prepared.localRsi, '--check-runtime'],
        { encoding: 'utf8', timeout: 60000, maxBuffer: 1024 * 1024, env: {} });
      await save('runtime-check.json', { passed: runtime.status === 0, stdout: runtime.stdout, stderr: runtime.stderr, error: runtime.error?.message ?? null });
      if (runtime.error || runtime.status !== 0) throw new Error(`NPU environment check failed; inspect ${output}/runtime-check.json`);
    }
    options.signal?.throwIfAborted();
    if (prepared.mode === 'optimize') {
      const { runGoal } = await import('./goal.mjs');
      await runGoal({ bundle, input: output, loopx: prepared.loopx, signal: options.signal });
      const result = JSON.parse(await readFile(join(output, 'sdk/result.json'), 'utf8'));
      await save('result.json', result);
      const state = JSON.parse(await readFile(join(output, 'sdk/state.json'), 'utf8'));
      const accepted = ['progress', 'completed'].includes(result.status) && result.observation?.validation?.status === 'passed' && state.pending_effect_ids?.length === 0;
      await save('acceptance.json', { accepted, pending_effect_ids: state.pending_effect_ids ?? null, scope: 'Original LoopX independent validator', validation: result.observation?.validation ?? null });
      if (!accepted) throw new Error(`Goal validation requires attention: ${result.status}; inspect ${output}`);
      return { status: result.status, accepted, output };
    }
    const { KernelCat } = await import('../../source/apps/agent/src/sdk/index.ts');
    const client = new KernelCat({ python: prepared.python, expectedPythonModules: prepared.expectedPythonModules,
      config: join(output, 'config.json'), timeoutMs: 7200000, maxFrameBytes: 1048576 });
    events = openSync(join(output, 'events.jsonl'), 'wx', 0o600);
    let sequence = 0;
    const result = await client.runTask({ task: join(output, 'task.json'), signal: options.signal,
      onEvent: event => writeSync(events, encode({ sequence: ++sequence, time: new Date().toISOString(), event }).replace(/\n\s*/g, '') + '\n') });
    await save('result.json', result);
    const check = spawnSync(prepared.python, ['-B', join(here, 'validate.py'), '--output', output],
      { encoding: 'utf8', timeout: 30000, maxBuffer: 1024 * 1024, env: process.env });
    let acceptance;
    try { acceptance = JSON.parse(check.stdout); }
    catch { acceptance = { accepted: false, error: check.error?.message || check.stderr || 'No validator result' }; }
    await save('acceptance.json', acceptance);
    if (check.status !== 0 || acceptance.accepted !== true) throw new Error(`Independent acceptance failed; inspect ${output}/acceptance.json`);
    return { status: result.status, accepted: true, output, events: sequence };
  } catch (error) {
    primaryError = error;
    await save('failure.json', { error: String(error), automatic_retry: false });
    throw error;
  } finally {
    if (events !== undefined) closeSync(events);
    if (oldPythonPath === undefined) delete process.env.PYTHONPATH;
    else process.env.PYTHONPATH = oldPythonPath;
    try {
      const { indexRun } = await import('./export.mjs');
      await indexRun(output);
    } catch (error) {
      await save('archive-failure.json', { error: String(error), execution_error: primaryError ? String(primaryError) : null });
      if (!primaryError) throw error;
    }
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    if (process.argv.includes('--help')) process.stdout.write(HELP);
    else if (process.argv.includes('--config')) await import('./run-legacy.mjs');
    else {
      const options = {};
      for (let i = 2; i < process.argv.length; i++) {
        const name = process.argv[i];
        if (name === '--prepare-only') { options.prepareOnly = true; continue; }
        if (!['--task', '--profile', '--output', '--mode'].includes(name) || !process.argv[i + 1] || options[name.slice(2)] !== undefined) throw new Error(HELP);
        options[name.slice(2)] = process.argv[++i];
      }
      if (!options.task || !options.profile || !options.output) throw new Error(HELP);
      process.stdout.write(JSON.stringify(await runCannTask(options)) + '\n');
    }
  } catch (error) {
    process.stderr.write(String(error.message) + '\n'); process.exitCode = 1;
  }
}
