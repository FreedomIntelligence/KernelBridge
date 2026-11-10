/** Customer SDK entrypoint. Prepare inputs with prepare_goal.py first. */
import { spawnSync } from 'node:child_process';
import { closeSync, openSync, writeSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { dirname, isAbsolute, join, relative, resolve } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';

const HELP = 'Usage: node examples/cann/goal.mjs --bundle ABS --input ABS [--loopx ABS]\n'
  + 'Consumes frozen config, task, command and TaskContract files from INPUT. Starts one bounded managed SDK Trial.\n'
  + 'Requires the prepared provider credential in its named environment variable.\n'
  + 'Writes create-only INPUT/sdk; never automatically retries a failed invocation.\n';

function argumentsFor(args) {
  if (args.length === 1 && ['--help', '-h'].includes(args[0])) return null;
  const values = new Map();
  for (let index = 0; index < args.length; index += 2) {
    const name = args[index];
    const value = args[index + 1];
    if (!['--bundle', '--input', '--loopx'].includes(name) || values.has(name) || !value || !isAbsolute(value)) {
      throw new Error('Expected each of --bundle ABS and --input ABS exactly once');
    }
    values.set(name, resolve(value));
  }
  if (!values.has('--bundle') || !values.has('--input')) throw new Error('Required arguments: --bundle ABS --input ABS');
  return { bundle: values.get('--bundle'), input: values.get('--input'), loopx: values.get('--loopx') };
}

async function readJson(path) {
  const text = await readFile(path, 'utf8');
  if (Buffer.byteLength(text) > 16 * 1024 * 1024) throw new Error(`Input exceeds 16 MiB: ${path}`);
  return JSON.parse(text);
}

function commandOptions(command) {
  const argv = command.argv;
  if (!Array.isArray(argv) || argv.length < 3 || argv[2] !== 'run'
      || argv.some(value => typeof value !== 'string') || !isAbsolute(argv[0]) || !isAbsolute(argv[1])
      || typeof command.python_path !== 'string' || !isAbsolute(command.python_path)
      || typeof command.api_key_env !== 'string' || !/^[A-Z][A-Z0-9_]*$/.test(command.api_key_env)) {
    throw new Error('command.json must be original prepare_goal.py output');
  }
  const flags = new Map();
  const allowed = ['--python', '--expected-python-modules', '--config', '--task', '--timeout-ms'];
  for (let index = 3; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!allowed.includes(name) || flags.has(name) || value === undefined) throw new Error('Invalid prepared argv');
    flags.set(name, value);
  }
  for (const name of allowed.slice(0, 4)) {
    if (!isAbsolute(flags.get(name) ?? '')) throw new Error(`Prepared ${name} must be absolute`);
  }
  const timeoutMs = Number(flags.get('--timeout-ms'));
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 7200000) throw new Error('Invalid prepared timeout');
  return { node: argv[0], main: argv[1], agent: dirname(dirname(argv[1])),
    python: flags.get('--python'), expectedPythonModules: flags.get('--expected-python-modules'),
    config: flags.get('--config'), task: flags.get('--task'), timeoutMs };
}

function validatePrepared(config, task) {
  if (!Number.isInteger(config.policy.max_steps) || config.policy.max_steps < 1 || !config.rsi
      || !task.seed_files?.['solution.py']) throw new Error('Expected a bounded RSI config and solution.py seed');
}

export async function runGoal({ bundle, input, loopx, signal }) {
  signal?.throwIfAborted();
  if (!relative(bundle, input).startsWith('..')) throw new Error('--input must be outside --bundle');
  const command = await readJson(join(input, 'command.json'));
  const options = commandOptions(command);
  const [config, task, contract] = await Promise.all([
    readJson(options.config), readJson(options.task), readJson(join(input, 'task-contract.json')),
  ]);
  validatePrepared(config, task);
  const apiKeyEnv = config.providers.primary.api_key_env;
  if (typeof apiKeyEnv !== 'string' || !/^[A-Z][A-Z0-9_]*$/.test(apiKeyEnv)) {
    throw new Error('Prepared primary provider must name its credential environment variable');
  }
  const secret = process.env[apiKeyEnv];
  if (!secret) throw new Error(`Export ${apiKeyEnv} before running the managed example`);
  const redact = value => JSON.stringify(value, null, 2).split(secret).join('[REDACTED]') + '\n';
  const output = join(input, 'sdk');
  await mkdir(output); // The attempt reservation precedes account creation; never redispatch on loss.
  const save = (name, value) => writeFile(join(output, name), redact(value), { flag: 'wx', mode: 0o600 });
  process.env.PYTHONPATH = [command.python_path, process.env.PYTHONPATH].filter(Boolean).join(process.platform === 'win32' ? ';' : ':');
  process.env.PYTHONDONTWRITEBYTECODE = '1';
  delete process.env.PYTHONPYCACHEPREFIX;
  const importSource = path => import(pathToFileURL(join(options.agent, path)).href);
  const [{ KernelCat, LoopXGoal }, { canonicalProgram, loadLaunchManifest }, { validateTaskContract }] = await Promise.all([
    importSource('src/sdk/index.ts'), importSource('src/tasks/launch-manifest.ts'), importSource('src/tasks/contract.ts'),
  ]);
  validateTaskContract(contract);
  const program = canonicalProgram({ schema_version: 'kernelcat.program-bundle/v1', entrypoint: 'solution.py',
    configuration_source: '{}', files: Object.entries(task.seed_files).map(([path,text]) => ({path,text})) });
  contract.baseline.artifact_digest = program.digest;
  const identity = randomUUID();
  const receipt = await createAccount(options, config, input, output, identity, save);
  const objective = task;
  const manifest = launchManifest(receipt, identity, config, objective, contract, program);
  await save('manifest.json', manifest);
  await loadLaunchManifest(join(output, 'manifest.json'));
  await save('task-contract.json', contract);
  await writeFile(join(output, 'baseline.py'), task.seed_files['solution.py'], { flag: 'wx', mode: 0o600 });
  signal?.throwIfAborted();
  await runSession(KernelCat, LoopXGoal, options, output, manifest, save, secret, loopx, signal);
}

async function createAccount(options, config, input, output, identity, save) {
  const intent = `cann-task-account-${identity}`;
  await save('account-intent.json', { intent_id: intent, credits: config.credits, multi_trial: true });
  const account = spawnSync(options.node, [options.main, 'account-create', '--python', options.python,
    '--expected-python-modules', options.expectedPythonModules, '--config', options.config,
    '--intent', intent, '--credits', String(config.credits), '--multi-trial',
    '--timeout-ms', String(options.timeoutMs)], {
    cwd: input, env: process.env, encoding: 'utf8', timeout: options.timeoutMs, maxBuffer: 4 * 1024 * 1024,
  });
  await save('account-command.json', { exit_code: account.status, signal: account.signal,
    stdout: account.stdout, stderr: account.stderr, error: account.error?.message ?? null });
  if (account.error || account.status !== 0) throw new Error(`Account outcome requires reconciliation; inspect ${output}`);
  const receipt = JSON.parse(account.stdout);
  await save('account.json', receipt);
  return receipt;
}

function launchManifest(receipt, identity, config, task, contract, program) {
  return {
    schema_version: 'kernelcat.run-launch/v1', account_id: receipt.account.account_id,
    intent_id: `cann-task-launch-${identity}`, run_id: `cann-task-run-${identity}`,
    trial_id: `cann-task-trial-${identity}`, branch_id: `cann-task-branch-${identity}`,
    session_id: `cann-task-session-${identity}`, seed: 17, goal: task.goal,
    contract_source_text: JSON.stringify(contract), program: program.bundle,
    proposal: { proposal_id: `cann-task-proposal-${identity}`, conditions: 'Pinned baseline solution.py is present',
      operation: 'Generate, measure and optimize the declared native CANN operator',
      expected_outcome: 'A candidate source with original independent RSI receipts, or an explicit unresolved result',
      stop_conditions: ['Configured model-turn budget exhausted or Pi finishes'], parameter_source: '{}' },
    model_route: 'primary', run_credit_limit: config.credits, trial_credit_limit: config.credits,
  };
}

async function runSession(KernelCat, LoopXGoal, options, output, manifest, save, secret, loopx, signal) {
  const client = new KernelCat({ python: options.python, expectedPythonModules: options.expectedPythonModules,
    config: options.config, timeoutMs: options.timeoutMs, maxFrameBytes: 1048576 });
  const session = await client.createSession({ manifest: join(output, 'manifest.json') });
  const events = openSync(join(output, 'events.jsonl'), 'wx', 0o600);
  let bytes = 0;
  let sequence = 0;
  try {
    if (loopx) {
      await advanceGoal(LoopXGoal, session, options, output, manifest, save, secret, events, loopx, signal);
      return;
    }
    const invocation = session.runStreamed({ signal });
    void invocation.result.catch(() => {}); // Event failure still settles/disposes the original invocation.
    for await (const event of invocation.events) {
      if (event.sequence !== ++sequence) throw new Error('SDK event order changed');
      const line = JSON.stringify(event).split(secret).join('[REDACTED]') + '\n';
      bytes += Buffer.byteLength(line);
      if (bytes > 32 * 1024 * 1024) throw new Error('SDK event log exceeded 32 MiB; inspect retained receipts');
      writeSync(events, line);
    }
    const result = await invocation.result;
    await save('result.json', result);
    const state = await session.state();
    await save('state.json', state);
    process.stdout.write(JSON.stringify({ status: result.status, output, events: sequence,
      run_id: manifest.run_id, trial_id: manifest.trial_id, pending_effect_ids: state.pending_effect_ids }) + '\n');
    if (result.status !== 'finished') process.exitCode = 1;
  } finally {
    closeSync(events);
    await session.dispose();
  }
}

async function advanceGoal(LoopXGoal, session, options, output, manifest, save, secret, events, cli, signal) {
  const root = join(output, 'loopx');
  const connection = { cli, node: options.node, project: join(root, 'project'),
    registry: join(root, 'registry.md'), runtimeRoot: join(root, 'runtime'), home: join(root, 'home'),
    timeoutMs: Math.min(options.timeoutMs, 3600000) };
  for (const path of [connection.project, connection.runtimeRoot, connection.home]) await mkdir(path, { recursive: true });
  const goalId = manifest.run_id;
  const agentId = 'kernelcat-sdk';
  const creation = await LoopXGoal.create(connection, { goalId, objective: manifest.goal,
    stateFile: join(connection.project, 'goal.md') });
  await save('goal-create.json', creation);
  const { LoopXClient } = await import(pathToFileURL(join(options.agent, 'src/sdk/loopx/client.ts')).href);
  const owner = await LoopXClient.connect(connection);
  await save('goal-sync.json', await owner.call(['sync-global', '--goal-id', goalId]));
  await save('goal-policy.json', await owner.call(['configure-goal', '--goal-id', goalId,
    '--spawn-allowed', '--max-children', '1', '--execute']));
  await save('goal-agent.json', await owner.call(['register-agent', '--goal-id', goalId,
    '--agent-id', agentId, '--require-new', '--execute']));
  const todo = await owner.call(['todo', 'add', '--goal-id', goalId, '--role', 'agent',
    '--text', 'Optimize the declared CANN operator with original native receipts', '--claimed-by', agentId]);
  await save('goal-todo.json', todo);
  const todoId = todo.todo_id;
  if (typeof todoId !== 'string') throw new Error('Original LoopX Todo response omitted todo_id');
  const validator = join(output, 'validate-goal.py');
  await writeFile(validator, VALIDATOR, { flag: 'wx', mode: 0o600 });
  const goal = await LoopXGoal.bind({ ...connection, goalId, agentId, todoId,
    applicationSessionId: session.applicationSessionId, validatorTimeoutSeconds: 30,
    validatorArgv: [options.python, '-B', '-P', validator, options.config, output] }, session);
  await save('goal-before.json', await goal.read());
  let observerError;
  let count = 0;
  let bytes = 0;
  const unsubscribe = session.subscribe(event => {
    try {
      const line = JSON.stringify(event).split(secret).join('[REDACTED]') + '\n';
      bytes += Buffer.byteLength(line);
      if (bytes > 32 * 1024 * 1024) throw new Error('SDK event log exceeded 32 MiB');
      writeSync(events, line);
      count += 1;
    } catch (error) { observerError = error; session.interrupt('Event observer failed'); }
  });
  try {
    const result = await goal.advance({ intentId: `cann-task-turn-${manifest.run_id}`, signal, run: { signal } });
    await save('result.json', result);
    if (result.turnKey) await save('goal-turn.json', await goal.inspectTurn(result.turnKey));
    await save('goal-after.json', await goal.read());
    const state = await session.state();
    await save('state.json', state);
    if (observerError) throw observerError;
    process.stdout.write(JSON.stringify({ status: result.status, output, events: count,
      goal_id: goalId, turn_key: result.turnKey, run_id: manifest.run_id,
      trial_id: state.stamp.trial_id, pending_effect_ids: state.pending_effect_ids }) + '\n');
    if (!['progress', 'completed'].includes(result.status)) process.exitCode = 1;
  } finally { unsubscribe(); }
}

const VALIDATOR = String.raw`"""Finite independent validation of original retained owner evidence."""
import ast
import hashlib
import json
import sys
from pathlib import Path
from kernelcat_executor.state.runtime import RuntimeJournal

config = json.loads(Path(sys.argv[1]).read_text())
output = Path(sys.argv[2])
manifest = json.loads((output / 'manifest.json').read_text())
journal = RuntimeJournal(Path(config['state_dir']) / 'events.jsonl')
snapshot = journal.snapshot()
actions = [row for row in snapshot['actions'].values()
           if row.get('binding', {}).get('run_id') == manifest['run_id']]
completed = [row for row in actions if row.get('outcome', {}).get('status') == 'completed']
audits = [row for row in completed if row['action']['tool'] == 'delegate'
          and row['action']['arguments'].get('agent') == 'kernel-reviewer'
          and row['action']['arguments'].get('mode') == 'read_only']
# No child delegation is requested in this 40-turn search.
measured = []
for row in completed:
    if row['action']['tool'] not in ('__rsi_record__', '__rsi_apply__'):
        continue
    reference = row['outcome']['data'].get('private_reference')
    if reference is None:
        continue
    retained = json.loads(Path(reference['locator']).read_text())
    if retained['kind'] == 'node' and retained['value'].get('correct') is True:
        measured.append(retained['value'])
assert measured, 'No original correctness-passing measured RSI candidate'
state = journal.state(manifest['run_id'], manifest['trial_id'])
reference = state['materialization']['result']['manifest_ref']
source = Path(reference['locator']).read_bytes()
assert len(source) == reference['byte_length'], 'Materialized candidate byte length changed'
assert 'sha256:' + hashlib.sha256(source).hexdigest() == reference['digest'], 'Materialized candidate digest changed'
files = [f for f in json.loads(source)['files'] if f['path'] == 'solution.py']
assert len(files) == 1, 'Expected one native source candidate'
tree = ast.parse(files[0]['text'])
names = {target.id for statement in tree.body if isinstance(statement, ast.Assign)
         for target in statement.targets if isinstance(target, ast.Name)}
assert {'project_json_src', 'host_tiling_src', 'host_operator_src', 'kernel_src',
        'python_bind_src', 'model_src'} <= names, 'Retained program is not a native six-source candidate'
def source_digest(value):
    canonical = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()
actual_digest = source_digest(files)
# Correctness must bind these exact native bytes, never a Torch-only fallback.
assert any(node['source_digest'] == actual_digest and node['retained_source_digest'] == actual_digest
           for node in measured), 'Materialized candidate has no matching correct original RSI measurement'
assert not state['pending_effect_ids'], 'Original effects remain unresolved'
print(json.dumps({'ok': True, 'audit_receipts': len(audits), 'correct_measured_nodes': len(measured)}))
`;

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) try {
  const args = argumentsFor(process.argv.slice(2));
  if (args === null) process.stdout.write(HELP);
  else await runGoal(args);
} catch (error) {
  process.stderr.write(`cann-task SDK: ${String(error.message).split(process.env.DEEPSEEK_API_KEY || '\0').join('[REDACTED]')}; preserve original state; no automatic retry\n`);
  process.exitCode = 1;
}
