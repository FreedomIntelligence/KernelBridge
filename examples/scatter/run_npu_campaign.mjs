/** Run/resume 20 actual model-led NPU optimization rounds with durable receipts. */
import { readFile, writeFile, rename, copyFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID, createHash } from 'node:crypto';
import { codegenSettings, startCodegen } from '../../source/apps/agent/src/application/codegen.mjs';
import { attachApplication } from '../../source/apps/agent/src/application/host.mjs';
import { ApplicationSession } from '../../source/apps/agent/src/sdk/application-session.mjs';

const args = process.argv.slice(2);
function option(name) {
  const index = args.indexOf(name);
  if (index < 0 || !args[index + 1] || args[index + 1].startsWith('--')) throw new Error(`${name} required`);
  return args[index + 1];
}
const root = resolve(option('--campaign'));
const configPath = resolve(option('--config'));
const repo = fileURLToPath(new URL('../../', import.meta.url));
const json = async path => JSON.parse(await readFile(path, 'utf8'));
async function optional(path) {
  try { return await json(path); } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}
async function save(path, value) {
  const temporary = `${path}.${process.pid}.tmp`;
  await writeFile(temporary, JSON.stringify(value, null, 2) + '\n', { mode: 0o600 });
  await rename(temporary, path);
}
const python = (await readFile(join(repo, '.development/python'), 'utf8')).trim();
const expectedPythonModules = (await readFile(join(repo, '.development/expected'), 'utf8')).trim();
process.env.PYTHONPATH = (await readFile(join(repo, '.development/pythonpath'), 'utf8')).trim();
const trial = await json(join(root, 'trial-config.json'));
const record = join(root, 'session.json');
const previous = await optional(record);
let session;
if (previous) {
  session = new ApplicationSession(await attachApplication(previous.application_session_id, { restart: true }));
} else {
  const config = await json(configPath);
  config.business_tools = [await json(join(root, 'business-tool.json'))];
  delete config.operator_pipeline; delete config.operator_infra;
  config.subagents = { enabled: false };
  const path = join(root, 'model-config.json');
  await save(path, config);
  const options = { config: path, cwd: join(root, 'project'), output: join(root, 'host'), python, expectedPythonModules };
  const settings = await codegenSettings(options);
  session = new ApplicationSession(await startCodegen(options, settings, {}));
  await save(record, { application_session_id: session.applicationSessionId, mode: trial.mode });
}

const rows = [];
let best = null;
async function retain(round, result) {
  const source = join(root, 'trials', `round-${String(round).padStart(2, '0')}`, 'candidate.py');
  const sha = createHash('sha256').update(await readFile(source)).digest('hex');
  if (sha !== result.source_sha256) throw new Error(`Round ${round} source differs from its receipt`);
  if (!result.original_handle || !result.remote_result_sha256) throw new Error(`Round ${round} lacks an original device receipt`);
  rows.push(result);
  if (result.correct && Number.isFinite(result.speedup) && (!best || result.speedup > best.speedup)) {
    best = { round, speedup: result.speedup, source_sha256: sha, source };
    await copyFile(source, join(root, 'best-observed.py'));
  }
  // Continue from a correct candidate, while preserving every failed attempt.
  if (best) await copyFile(best.source, trial.candidate);
  await save(join(root, 'project/feedback.json'), { last: result, best_observed: best,
    qualification: 'Each round pairs against eager; comparisons between rounds are not paired promotion.' });
  const state = await session.state();
  await save(join(root, 'progress.json'), { rounds: rows.length, mode: trial.mode, best_observed: best,
    application_session_id: session.applicationSessionId, model_calls: state.model_turns,
    usage: state.budget?.spent, results: rows });
}

try {
  for (let round = 1; round <= 20; round++) {
    const folder = join(root, 'trials', `round-${String(round).padStart(2, '0')}`);
    const resultPath = join(folder, 'result.json');
    const completed = await optional(resultPath);
    if (completed) { await retain(round, completed); continue; }
    if (await optional(join(folder, 'intent.json'))) {
      throw new Error(`Round ${round} already has an original device intent. Recover with npu_trial.py TRIAL_CONFIG --recover ${round}; never launch a replacement.`);
    }
    const state = await session.state();
    if (state.pending_effect_ids.length || Object.keys(state.calls).length) throw new Error('Original effects unresolved; recover before continuing');
    const existing = await optional(join(root, 'trials/requested.json'));
    if (existing?.round === round && state.inputs?.[existing.input_id]) {
      throw new Error(`Round ${round} already has input ${existing.input_id} without a device receipt. Inspect/resume the original input before continuing.`);
    }
    const inputId = `optimization-${trial.mode}-${round}-${randomUUID()}`;
    await save(join(root, 'trials/requested.json'), { round, input_id: inputId });
    const prompt = `Optimization round ${round}/20 for feature-index scatter ${trial.mode} on Ascend NPU. Read AGENTS.md, candidate.py, contract.json and feedback.json if present. Propose ONE concrete new structural optimization grounded in the prior measurements and diagnostics. Edit candidate.py without changing its six-source format, ABI or semantics. Then call npu_trial with round=${round} and a falsifiable hypothesis, exactly once. The tool freezes the changed source, compiles on the NPU host, checks correctness and measures same-device eager/candidate/candidate/eager pairs. Keep every failure as evidence. Do not run another benchmark or device command, alter the oracle, merely add comments, or rename variables. Inspect previous errors and avoid repeating failed mechanisms. After the tool returns, summarize the measured result and stop. The coordinator will request the next round. Evidence is NPU local paired timing, not an official result; across-round ranking is only best observed.`;
    await session.prompt(prompt, { inputId });
    console.log(JSON.stringify({ event: 'round_started', round, operator: trial.mode, input_id: inputId }));
    await session.run();
    const result = await optional(resultPath);
    if (!result) throw new Error(`Round ${round} has no evaluated candidate; inspect original session before submitting another prompt`);
    await retain(round, result);
    console.log(JSON.stringify({ event: 'round_finished', round, operator: trial.mode,
      correct: result.correct, speedup: result.speedup, error: result.error, source_sha256: result.source_sha256 }));
  }
  console.log(JSON.stringify({ event: 'campaign_completed', operator: trial.mode, rounds: rows.length, best_observed: best }));
} finally { session.dispose(); }
