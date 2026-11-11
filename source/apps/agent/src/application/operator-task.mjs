/** Compile a clean, implementation-free operator task into an isolated application. */
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, readFile, realpath, writeFile } from 'node:fs/promises';
import { dirname, isAbsolute, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const save = (path, value) => writeFile(path, JSON.stringify(value, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
const object = value => value && typeof value === 'object' && !Array.isArray(value);
function paths(values, name) {
  if (!Array.isArray(values) || !values.length || new Set(values).size !== values.length
      || values.some(path => typeof path !== 'string' || !path || isAbsolute(path) || path.split('/').some(part => !part || part === '.' || part === '..'))) {
    throw new Error(`${name} must contain distinct relative paths`);
  }
  return values;
}

export async function prepareOperatorTask({ task, selected, output, agentRoot }) {
  if (task.schema_version !== 'kernelbridge.operator-task/v1' || selected.schema_version !== 'kernelbridge.operator-profile/v1') {
    throw new Error('Operator tasks require kernelbridge.operator-task/v1 and kernelbridge.operator-profile/v1');
  }
  if (Object.keys(task).some(key => !['schema_version', 'instruction', 'contract', 'files', 'writable', 'readonly'].includes(key))
      || typeof task.instruction !== 'string' || !task.instruction.trim() || !object(task.contract) || !object(task.files)) {
    throw new Error('Clean operator task requires instruction, contract and document files');
  }
  const writable = paths(task.writable, 'writable');
  const readonly = paths(task.readonly, 'readonly');
  if (Object.entries(task.files).some(([path, value]) => !readonly.includes(path) || typeof value !== 'string')
      || readonly.some(path => !Object.hasOwn(task.files, path))
      || writable.some(path => readonly.some(ro => path === ro || path.startsWith(ro + '/') || ro.startsWith(path + '/')))) {
    throw new Error('Clean tasks may seed readonly documents only; writable candidate sources must start absent');
  }
  const config = structuredClone(selected.config ?? {});
  if (await realpath(dirname(output)) !== dirname(output)) throw new Error('Trial output must not traverse symlinks');
  if (config.workspace_mode === 'project' || config.native_session_seed || config.rsi || config.evaluator || config.candidate_retention) {
    throw new Error('Clean operator trials require a fresh isolated workspace and explicit pipeline tools');
  }
  const tools = config.business_tools ?? [];
  if (!['pipeline_run', 'pipeline_pair', 'pipeline_promote'].every(name => tools.some(tool => tool.name === name))) {
    throw new Error('Operator profile must provide F, G and H pipeline tools');
  }
  const pins = [];
  async function sourceHashes(files) {
    if (!object(files) || !Object.keys(files).length) throw new Error('Acceptance requires pinned oracle and baseline files');
    const hashes = {};
    paths(Object.keys(files), 'source names');
    for (const [name, path] of Object.entries(files)) {
      if (typeof path !== 'string' || !isAbsolute(path) || await realpath(path) !== path) throw new Error('Acceptance source paths must be canonical absolute files');
      hashes[name] = sha(await readFile(path)); pins.push({ path, sha256: hashes[name] });
    }
    return hashes;
  }
  const contract = structuredClone(task.contract);
  const pipelineContract = structuredClone(contract);
  pipelineContract.oracle.files = await sourceHashes(contract.oracle?.files);
  const baseline = await sourceHashes(selected.acceptance?.baseline_files);
  if (typeof selected.acceptance?.scope !== 'string' || !selected.acceptance.scope) throw new Error('Acceptance requires the actual driver evidence scope');
  contract.acceptance = { kind: 'operator_pipeline', tool: 'pipeline_promote', evidence_tier: 'E3',
    source_paths: writable, pipeline_contract: pipelineContract, baseline_files: baseline, scope: selected.acceptance.scope };
  for (const tool of tools) {
    tool.files = [...tool.files, ...pins.filter(pin => !tool.files.some(existing => existing.path === pin.path))];
  }
  config.application = { ...selected.application, max_trials: selected.goal?.max_trials ?? selected.application?.max_trials,
    complete_on_acceptance: selected.goal?.complete_on_acceptance ?? true };
  const bindings = JSON.parse(await readFile(selected.expected_python_modules, 'utf8'));
  const roots = new Set(bindings.modules.flatMap(module => module.allowed_package_roots.map(root => dirname(fileURLToPath(root)))));
  if (roots.size !== 1) throw new Error('Expected one frozen installed Python module root');
  Object.assign(config, { python_modules: bindings, workspace_mode: 'isolated', workspace: join(output, 'workspace'),
    state_dir: join(output, 'state'), writable_paths: writable, readonly_paths: readonly,
    pi_worker: { ...config.pi_worker, node: selected.node ?? process.execPath, root: agentRoot } });
  const compiled = { id: 'operator-' + randomUUID(), scope: 'operator-development', goal: task.instruction,
    contract, seed_files: task.files };
  await mkdir(output, { mode: 0o700 });
  await save(join(output, 'task.json'), compiled);
  await save(join(output, 'config.json'), config);
  await save(join(output, 'prepared.json'), { schema_version: task.schema_version, model_started: false, device_started: false,
    candidate_seeded: false, writable, readonly, model_turn_accounting: 'all_model_calls_including_compact_child_review' });
  return { pythonPath: [...roots][0] };
}
