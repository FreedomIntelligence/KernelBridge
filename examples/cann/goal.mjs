/** Customer SDK entrypoint. Prepare inputs with prepare_goal.py first. */
import { spawnSync } from 'node:child_process';
import { closeSync, openSync, writeSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { dirname, isAbsolute, join, relative, resolve } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { loadCodegenCredentials, configuredCredentialValues } from '../../source/apps/agent/src/application/credentials.mjs';
import { withProcessEnvironment } from '../../source/apps/agent/src/harness/pi/environment.mjs';

const HELP = 'Usage: node examples/cann/goal.mjs --bundle ABS --input ABS [--loopx ABS]\n'
  + 'Consumes frozen config, task, command and TaskContract files from INPUT. Starts a bounded LoopX pursuit with managed Pi Trials.\n'
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
  if (!Number.isInteger(config.policy.max_steps) || config.policy.max_steps < 1 || (!config.rsi && !config.candidate_retention)
      || !task.seed_files?.['solution.py']) throw new Error('Expected bounded RSI or source-bound functional acceptance and solution.py seed');
}

export async function runGoal({ bundle, input, loopx, signal }) {
  return withProcessEnvironment({}, async () => {
    const command = await readJson(join(input, 'command.json'));
    const config = await readJson(commandOptions(command).config);
    await loadCodegenCredentials(config.providers ?? {}, config.business_tools ?? []);
    const secrets = configuredCredentialValues(config.providers ?? {}, config.business_tools ?? []);
    try { return await scopedGoal({ bundle, input, loopx, signal }); }
    catch (error) {
      throw new Error(secrets.reduce((text, key) => text.split(key).join('[REDACTED]'), String(error.message ?? error)));
    }
  });
}

async function scopedGoal({ bundle, input, loopx, signal }) {
  const names = ['PYTHONPATH', 'PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX'];
  const previous = Object.fromEntries(names.map(name => [name, process.env[name]]));
  try { return await executeGoal({ bundle, input, loopx, signal }); }
  finally {
    for (const name of names) {
      if (previous[name] === undefined) delete process.env[name];
      else process.env[name] = previous[name];
    }
  }
}

async function executeGoal({ bundle, input, loopx, signal }) {
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
  const secrets = configuredCredentialValues(config.providers ?? {}, config.business_tools ?? []);
  const redactText = text => secrets.reduce((value, key) => value.split(key).join('[REDACTED]'), text);
  const redact = value => redactText(JSON.stringify(value, null, 2)) + '\n';
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
  await runSession(KernelCat, LoopXGoal, options, output, manifest, save, redactText, loopx, signal);
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
      operation: 'Implement and validate the frozen operator task with the configured tools',
      expected_outcome: 'A source-bound candidate with original independent acceptance receipts',
      stop_conditions: ['Configured model-turn budget exhausted or Pi finishes'], parameter_source: '{}' },
    model_route: 'primary', run_credit_limit: config.credits, trial_credit_limit: config.credits,
  };
}

async function runSession(KernelCat, LoopXGoal, options, output, manifest, save, redactText, loopx, signal) {
  const client = new KernelCat({ python: options.python, expectedPythonModules: options.expectedPythonModules,
    config: options.config, timeoutMs: options.timeoutMs, maxFrameBytes: 1048576 });
  const session = await client.createSession({ manifest: join(output, 'manifest.json') });
  const events = openSync(join(output, 'events.jsonl'), 'wx', 0o600);
  let bytes = 0;
  let sequence = 0;
  try {
    if (loopx) {
      await advanceGoal(LoopXGoal, session, options, output, manifest, save, redactText, events, loopx, signal);
      return;
    }
    const invocation = session.runStreamed({ signal });
    void invocation.result.catch(() => {}); // Event failure still settles/disposes the original invocation.
    for await (const event of invocation.events) {
      if (event.sequence !== ++sequence) throw new Error('SDK event order changed');
      const line = redactText(JSON.stringify(event)) + '\n';
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

async function advanceGoal(LoopXGoal, session, options, output, manifest, save, redactText, events, cli, signal) {
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
    '--spawn-allowed', '--max-children', '1', '--coordination-runtime-shadow-file', '--execute']));
  await save('goal-agent.json', await owner.call(['register-agent', '--goal-id', goalId,
    '--agent-id', agentId, '--require-new', '--execute']));
  await save('goal-authority-bootstrap.json', await owner.call(['coordination-shadow', 'bootstrap', '--goal-id', goalId, '--execute']));
  const todo = await owner.call(['todo', 'add', '--goal-id', goalId, '--role', 'agent',
    '--text', 'Complete the declared operator task with original native receipts', '--claimed-by', agentId]);
  await save('goal-todo.json', todo);
  const todoId = todo.todo_id;
  if (typeof todoId !== 'string') throw new Error('Original LoopX Todo response omitted todo_id');
  await save('goal-authority-plan.json', await owner.call(['coordination-shadow', 'promote', '--goal-id', goalId,
    '--minimum-operations', '1', '--handoff-mode-migration', 'preserve']));
  await save('goal-authority-promote.json', await owner.call(['coordination-shadow', 'promote', '--goal-id', goalId,
    '--reviewed-plan', join(output, 'goal-authority-plan.json'), '--execute']));
  const validator = join(output, 'validate-goal.py');
  await writeFile(validator, await readFile(new URL('./validate_goal.py', import.meta.url)), { flag: 'wx', mode: 0o600 });
  const workflow = await readJson(join(dirname(output), 'workflow.json')).catch(error => {
    if (error.code !== 'ENOENT') throw error;
    return { max_trials: 1, complete_on_acceptance: false };
  });
  const validatorArgv = [options.python, '-B', '-P', validator, options.config, output];
  const basis = await owner.call(['goal-acceptance', 'inspect', '--goal-id', goalId]);
  await save('goal-acceptance-before.json', basis);
  await save('goal-acceptance-contract.json', { scope: { kind: 'selected_work', todo_ids: [todoId] },
    objective: 'Deliver the source that passes the frozen operator task acceptance checks.', non_goals: [],
    criteria: [{ id: 'operator-acceptance', description: 'Original source-bound correctness and task measurement receipts.',
      validation_argv: validatorArgv, validation_timeout_seconds: 25, validation_files: [] }],
    bindings: [{ todo_id: todoId, criterion_ids: ['operator-acceptance'] }] });
  await save('goal-acceptance-configure.json', await owner.call(['goal-acceptance', 'configure', '--goal-id', goalId,
    '--document', join(output, 'goal-acceptance-contract.json'), '--expected-provider-revision', basis.provider_revision,
    '--operation-id', `acceptance-${goalId}`, '--execute']));
  const goal = await LoopXGoal.bind({ ...connection, goalId, agentId, todoId,
    applicationSessionId: session.applicationSessionId, validatorTimeoutSeconds: 30,
    validatorArgv }, session);
  await save('goal-before.json', await goal.read());
  let observerError;
  let count = 0;
  let bytes = 0;
  const unsubscribe = session.subscribe(event => {
    try {
      const line = redactText(JSON.stringify(event)) + '\n';
      bytes += Buffer.byteLength(line);
      if (bytes > 32 * 1024 * 1024) throw new Error('SDK event log exceeded 32 MiB');
      writeSync(events, line);
      count += 1;
    } catch (error) { observerError = error; session.interrupt('Event observer failed'); }
  });
  try {
    const { createStageRouterNext } = await import(pathToFileURL(join(options.agent, 'src/sdk/next-trial.ts')).href);
    const next = createStageRouterNext(session, { manifest: join(output, 'manifest.json'),
      intentId: `pursuit-${manifest.run_id}`, trialCreditLimit: manifest.trial_credit_limit, observeGoal: () => goal.read() });
    const turns = await goal.pursue({ first: { intentId: `cann-task-turn-${manifest.run_id}`, signal, run: { signal },
      completionCandidate: workflow.complete_on_acceptance }, maxTurns: workflow.max_trials,
      next: async (previous, index) => {
        const request = await next(previous, index);
        return 'stop' in request ? request : { ...request, signal, completionCandidate: workflow.complete_on_acceptance };
      } });
    await save('goal-turns.json', turns);
    const result = { ...turns.at(-1), pursuit_status: turns.at(-1).status === 'progress' ? 'invocation_limit' : turns.at(-1).status };
    await save('result.json', result);
    if (result.turnKey) await save('goal-turn.json', await goal.inspectTurn(result.turnKey));
    await save('goal-after.json', await goal.read());
    // A failed bootstrap has no frozen Run yet. Preserve the actual SDK failure
    // instead of replacing it with a secondary state-query exception.
    if (!['progress', 'completed', 'replayed'].includes(result.status)) {
      await save('state-observation.json', { available: session.latestState !== undefined,
        state: session.latestState ?? null, goal_status: result.status });
      if (observerError) throw observerError;
      process.exitCode = 1;
      throw new Error(`Goal ${result.status}; inspect ${output}/result.json and ${output}/events.jsonl`);
    }
    const state = await session.state();
    await save('state.json', state);
    if (observerError) throw observerError;
    process.stdout.write(JSON.stringify({ status: result.status, output, events: count,
      goal_id: goalId, turn_key: result.turnKey, run_id: manifest.run_id,
      trial_id: state.stamp.trial_id, pending_effect_ids: state.pending_effect_ids }) + '\n');
    if (!['progress', 'completed'].includes(result.status)) process.exitCode = 1;
  } finally { unsubscribe(); }
}


if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) try {
  const args = argumentsFor(process.argv.slice(2));
  if (args === null) process.stdout.write(HELP);
  else await runGoal(args);
} catch (error) {
  process.stderr.write(`cann-task SDK: ${String(error.message)}; preserve original state; no automatic retry\n`);
  process.exitCode = 1;
}
