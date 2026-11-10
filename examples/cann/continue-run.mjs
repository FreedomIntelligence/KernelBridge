/** Bind a verified archived result and its Pi history to a new local execution profile. */
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { prepareCannTask, executePrepared } from './run.mjs';
import { verifyArchive } from './archive.mjs';
import { inspectNativeSessionFile } from '../../source/apps/agent/src/replay/native-session.ts';
const digest = value => createHash('sha256').update(value).digest('hex');
const json = async path => JSON.parse(await readFile(path, 'utf8'));
const save = (path, value) => writeFile(path, JSON.stringify(value, null, 2) + '\n', { flag: 'wx', mode: 0o600 });

async function finalCandidate(archive, acceptance) {
  const { manifest, files } = archive;
  const stateRow = manifest.files.find(row => row.path === 'sdk/state.json');
  if (stateRow) {
    const state = JSON.parse(files.get(stateRow.path));
    if (!Array.isArray(state.pending_effect_ids) || state.pending_effect_ids.length) throw new Error('Reconcile original pending effects before continuation');
    const result = JSON.parse(files.get('sdk/result.json'));
    if (!['progress', 'completed'].includes(result.status) || result.observation?.validation?.status !== 'passed') throw new Error('Managed continuation requires a successful original Goal validation');
    const reference = state.materialization?.result?.manifest_ref;
    if (!reference) throw new Error('Archived managed state has no materialized candidate');
    const row = manifest.files.find(file => 'sha256:' + file.sha256 === reference.digest && file.bytes === reference.byte_length);
    if (!row) throw new Error('Materialized candidate bytes are missing from archive');
    const program = JSON.parse(files.get(row.path));
    const source = program.files.filter(file => file.path === 'solution.py');
    if (source.length !== 1) throw new Error('Expected one materialized solution.py');
    return source[0].text;
  }
  const row = manifest.files.find(file => file.path.startsWith('workspace/') && file.sha256 === acceptance.candidate_sha256);
  if (!row) throw new Error('Accepted final candidate bytes are missing from archive');
  return files.get(row.path).toString('utf8');
}

export async function prepareContinuation({ restored, profile, output, goal }) {
  const archive = await verifyArchive(restored);
  restored = archive.root;
  const report = await json(join(restored, 'restore.json'));
  const manifest = archive.manifest;
  const acceptance = JSON.parse(archive.files.get('acceptance.json'));
  if (!report.restored || acceptance.accepted !== true || acceptance.pending_effect_ids?.length) {
    throw new Error('Continuation requires a verified restored archive and settled original acceptance');
  }
  if (report.archive_manifest_sha256 !== digest(archive.manifestBytes)) throw new Error('Restored archive identity changed');
  const session = manifest.indices.pi_sessions.find(path => path === report.sessions?.[0]?.session);
  if (!session) throw new Error('Original session is not in the verified archive index');
  const identity = await inspectNativeSessionFile(join(restored, session));
  if (identity.sessionId !== report.sessions[0].session_id || identity.leafId !== report.sessions[0].leaf_id) throw new Error('Restored session identity changed');
  const candidate = await finalCandidate(archive, acceptance);
  const oldTask = JSON.parse(archive.files.get('task.json'));
  const input = join(restored, 'continuation-inputs');
  await mkdir(input, { mode: 0o700 });
  const objective = goal || oldTask.goal;
  await writeFile(join(input, 'baseline.py'), candidate, { flag: 'wx' });
  await writeFile(join(input, 'session.jsonl'), archive.files.get(session), { flag: 'wx', mode: 0o400 });
  if (oldTask.contract?.suite === 'CANNBench Season 1 Sigmoid, 20 cases') {
    await save(join(input, 'task.json'), { ...oldTask, goal: objective,
      seed_files: { ...oldTask.seed_files, 'solution.py': candidate } });
    const contractName = manifest.files.some(row => row.path === 'task-contract.json') ? 'task-contract.json' : 'sdk/task-contract.json';
    await save(join(input, 'task-contract.json'), JSON.parse(archive.files.get(contractName)));
  } else {
    const caseText = oldTask.seed_files?.['reference/case.json'];
    if (!caseText) throw new Error('Archived task lacks its frozen case.json');
    await writeFile(join(input, 'case.json'), caseText, { flag: 'wx' });
    await save(join(input, 'task.json'), { schema_version: 'kernelbridge.cann-task/v1',
      goal: objective, case_file: 'case.json', baseline_file: 'baseline.py' });
  }
  const prepared = await prepareCannTask({ task: join(input, 'task.json'), profile, output,
    mode: 'optimize', sessionSeed: join(input, 'session.jsonl') });
  await save(join(output, 'lineage.json'), { archive_manifest_sha256: digest(await readFile(join(restored, 'manifest.json'))),
    original_session_id: report.sessions[0].session_id, original_leaf_id: report.sessions[0].leaf_id,
    baseline_sha256: digest(candidate), original_acceptance: acceptance,
    execution: 'Fresh local Goal and budget with the archived conversation and final candidate' });
  return prepared;
}

export async function continueRun(options) {
  const prepared = await prepareContinuation(options);
  return options.prepareOnly ? prepared : executePrepared(prepared, options);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const options = {};
    for (let index = 2; index < process.argv.length; index++) {
      const flag = process.argv[index];
      if (flag === '--prepare-only') { options.prepareOnly = true; continue; }
      if (!['--restored', '--profile', '--output', '--goal'].includes(flag) || !process.argv[index + 1]) throw new Error('Expected --restored ABS --profile ABS --output NEW_ABS [--goal TEXT] [--prepare-only]');
      options[flag.slice(2)] = process.argv[++index];
    }
    console.log(JSON.stringify(await continueRun(options)));
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
