/** Run task.goal as the prompt of the product's Pi SDK session. */
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { openSync, writeSync, closeSync } from 'node:fs';
import { resolve, join, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { KernelCat } from '../../source/apps/agent/src/sdk/index.ts';

const help = 'node examples/cann/run.mjs --task ABS --config ABS --python ABS --expected-python-modules ABS --output NEW_ABS\n'
  + 'PYTHONPATH must select the frozen Python deployment; provider keys stay in environment variables.\n'
  + 'The trusted config supplies evaluator, budgets and model. Task.goal is passed to Pi as the prompt.\n';
if (process.argv.includes('--help')) { process.stdout.write(help); } else {
  const args = new Map();
  for (let i = 2; i < process.argv.length; i += 2) {
    const key = process.argv[i], value = process.argv[i + 1];
    if (!['--task','--config','--python','--expected-python-modules','--output'].includes(key)
        || args.has(key) || !value || !isAbsolute(value)) throw new Error(help);
    args.set(key, value);
  }
  if (args.size !== 5) throw new Error(help);
  const output = resolve(args.get('--output'));
  const task = JSON.parse(await readFile(args.get('--task'), 'utf8'));
  const config = JSON.parse(await readFile(args.get('--config'), 'utf8'));
  if (config.rsi) throw new Error('This example runs an ordinary task. Use the managed SDK + LoopX entry for an RSI goal.');
  const secrets = Object.values(config.providers ?? {}).map(p => process.env[p.api_key_env]).filter(Boolean);
  const encode = value => secrets.reduce((s, key) => s.split(key).join('[REDACTED]'), JSON.stringify(value)) + '\n';
  await mkdir(output); // Reserve before starting effects; never overwrite an attempt.
  config.workspace = join(output, 'workspace');
  config.state_dir = join(output, 'state');
  config.writable_paths = ['solution.py'];
  config.readonly_paths = ['reference'];
  await writeFile(join(output, 'task.json'), JSON.stringify(task), { flag: 'wx', mode: 0o600 });
  await writeFile(join(output, 'config.json'), JSON.stringify(config), { flag: 'wx', mode: 0o600 });
  const events = openSync(join(output, 'events.jsonl'), 'wx', 0o600);
  let sequence = 0;
  try {
    const client = new KernelCat({ python: args.get('--python'),
      expectedPythonModules: args.get('--expected-python-modules'), config: join(output, 'config.json'),
      timeoutMs: 7200000, maxFrameBytes: 1048576 });
    const result = await client.runTask({ task: join(output, 'task.json'),
      onEvent: event => writeSync(events, encode({ sequence: ++sequence, time: new Date().toISOString(), event })) });
    await writeFile(join(output, 'result.json'), encode(result), { flag: 'wx', mode: 0o600 });
    const check = spawnSync(args.get('--python'), ['-B', fileURLToPath(new URL('./validate.py', import.meta.url)),
      '--output', output], { env: process.env, encoding: 'utf8', timeout: 30000, maxBuffer: 1024 * 1024 });
    let acceptance;
    try { acceptance = JSON.parse(check.stdout); }
    catch { acceptance = { accepted: false, error: check.error?.message ?? check.stderr ?? 'Validator returned no JSON' }; }
    await writeFile(join(output, 'acceptance.json'), encode(acceptance), { flag: 'wx', mode: 0o600 });
    const accepted = check.status === 0 && acceptance.accepted === true;
    process.stdout.write(encode({ status: result.status, accepted, events: sequence, output }));
    if (!accepted) process.exitCode = 1;
  } catch (error) {
    await writeFile(join(output, 'failure.json'), encode({ error: String(error), automatic_retry: false }), { flag: 'wx', mode: 0o600 });
    process.stderr.write(encode({ error: String(error), output, automatic_retry: false }));
    process.exitCode = 1;
  } finally { closeSync(events); }
}
