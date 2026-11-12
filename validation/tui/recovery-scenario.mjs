import { StepTimeout, scaled } from './timing.mjs';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { connect } from 'node:net';
import { readFile, writeFile, chmod, stat } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { ApplicationSession } from '../../source/apps/agent/src/sdk/index.ts';
import { ApplicationClient } from '../../source/apps/agent/src/application/client.mjs';

const options = JSON.parse(await readFile(process.argv[2], 'utf8'));
let session = await ApplicationSession.create(options);
let hostPid, worker;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate, timeout = 30000) {
  const end = Date.now() + scaled(timeout);
  while (Date.now() < end) { if (await predicate()) return; await sleep(50); }
  throw new StepTimeout('Recovery scenario timed out after ' + scaled(timeout) + ' ms');
}
const events = async () => (await readFile(options.output + '/state/events.jsonl', 'utf8')).trim().split('\n').map(JSON.parse);
try {
  hostPid = (await session.state()).host_pid;
  const directory = session.client.directory;
  // Permission and token failures must not produce a worker or execution intent.
  await chmod(directory + '/host.sock', 0o666);
  await assert.rejects(ApplicationClient.connect(directory), /permissions/);
  await chmod(directory + '/host.sock', 0o600);
  const bad = connect(directory + '/host.sock');
  const response = await new Promise((resolve, reject) => {
    bad.once('error', reject);
    bad.setTimeout(2000, () => reject(new Error('Authentication did not terminate')));
    bad.once('end', () => resolve({ error: { message: 'invalid token connection closed' } }));
    bad.once('data', bytes => resolve(JSON.parse(bytes.toString())));
    bad.write(JSON.stringify({ id: 1, method: 'authenticate', params: { token: 'wrong' } }) + '\n');
  });
  bad.destroy();
  assert.match(response.error.message, /token/);
  const original = await session.prompt('Run the fixture once; retain this accepted input.', { inputId: 'original' });
  const { ticket } = await session.client.call('ticket', { mode: 'headless' });
  worker = spawn(process.execPath, [fileURLToPath(new URL('../../source/apps/agent/src/application/worker.mjs', import.meta.url)),
    '--host', directory, '--ticket', ticket, '--headless'], { stdio: ['ignore', 'ignore', 'pipe'], env: { PATH: '/usr/bin:/bin', PI_OFFLINE: '1' } });
  let stderr = '';
  worker.stderr.on('data', chunk => { stderr += chunk; });
  await until(async () => { try { await stat(options.output + '/evaluation-accepted'); return true; } catch { return false; } });
  await assert.rejects(session.client.call('ticket', { mode: 'interactive' }), /active worker/);
  const start = Date.now();
  await session.pause();
  assert.ok(Date.now() - start < 2000, 'control acknowledgement was blocked by the evaluator');
  const atKill = await session.state();
  assert.ok(atKill.pending_effect_ids.length);
  worker.kill('SIGKILL');
  await writeFile(options.output + '/evaluation-accepted.release', 'release original evaluation');
  await until(async () => !(await session.state()).lease);
  const settled = await session.state();
  assert.equal(settled.pending_effect_ids.length, 0);
  assert.equal(settled.inputs.original.status, 'applied');
  const modelCount = settled.model_turns;
  const spent = settled.budget.spent;
  // Simulate host-loss truncation, including the original assistant entry. The
  // durable original evaluation remains authoritative; no assistant is invented.
  const nativePath = settled.checkpoint.session_file;
  const lines = (await readFile(nativePath, 'utf8')).trimEnd().split('\n');
  const parsed = lines.map(JSON.parse);
  const assistant = parsed.findLastIndex(row => row.type === 'message' && row.message.role === 'assistant');
  assert.ok(assistant > 0);
  await writeFile(nativePath, lines.slice(0, assistant).join('\n') + '\n{"truncated":');
  const previousToken = await readFile(directory + '/token', 'utf8');
  session.dispose();
  process.kill(hostPid, 'SIGKILL');
  await until(() => { try { process.kill(hostPid, 0); return false; } catch { return true; } });
  session = await ApplicationSession.attach(original.application_session_id ?? settled.application_session_id, { restart: true });
  hostPid = (await session.state()).host_pid;
  assert.notEqual(await readFile(directory + '/token', 'utf8'), previousToken);
  let recovered = await session.run();
  assert.equal(recovered.model_turns, modelCount);
  assert.equal(recovered.budget.spent, spent);
  assert.equal(recovered.inputs.original.status, 'interrupted');
  assert.equal(await readFile(options.output + '/evaluation-accepted', 'utf8'), 'accepted\n');
  const repaired = await readFile(recovered.checkpoint.session_file, 'utf8');
  assert.match(repaired, /kernelbridge-recovered/);
  assert.match(repaired, /original_receipt/);
  await session.prompt('Continue after recovery without repeating the evaluation.', { inputId: 'continue' });
  recovered = await session.run();
  assert.equal(recovered.inputs.continue.status, 'completed');
  assert.equal(recovered.model_turns, modelCount + 1);
  assert.equal((await events()).filter(row => row.kind === 'admitted' && row.action.tool === 'evaluate_candidate').length, 1);
  assert.equal(await readFile(options.output + '/evaluation-accepted', 'utf8'), 'accepted\n');
  await writeFile(options.output + '/scenario.json', JSON.stringify({ status: 'passed', state: recovered, worker_stderr: stderr }, null, 2));
  process.stdout.write(JSON.stringify({ status: 'passed', evaluations: 1, model_turns: recovered.model_turns, spent: recovered.budget.spent }) + '\n');
} finally {
  session.dispose();
  worker?.kill('SIGKILL');
  if (hostPid) { try { process.kill(hostPid, 'SIGTERM'); } catch {} }
}
