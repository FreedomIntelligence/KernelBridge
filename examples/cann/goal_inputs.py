"""Prepare the existing managed SDK/LoopX protocol from frozen CANN inputs."""
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def task_contract(task, reference):
    contract = json.loads((ROOT / 'examples/cann/goal-contract.template.json').read_text())
    case = task['contract']; body = json.dumps(case, sort_keys=True).encode()
    contract.update(contract_id=task['id'] + '-goal-v1')
    contract['provenance'] = {'kind': 'frozen_inputs', 'source': 'CANN task case.json and frozen reference', 'source_digest': 'sha256:' + sha(body)}
    contract['operator'] = {'name': case['operator'], 'definition': case['semantics'], 'reference_digest': 'sha256:' + sha(reference.encode())}
    contract['target'] = {'vendor': 'Huawei', 'architecture': case['target']['soc'].removeprefix('ai_core-'), 'requirements': {'runtime': 'CANN 9.1, torch_npu 2.10, logical device ' + str(case['target']['device'])}}
    clean = lambda row: {k: v for k, v in row.items() if k in ('shape', 'dtype')} | {'layout': 'contiguous'}
    contract['input_domain'] = {'casepack_digest': 'sha256:' + sha(body), 'cases': {'case1': {'weight': 1,
        'inputs': {row['name']: clean(row) for row in case['inputs']}, 'outputs': {'y': clean(case['output'])}}}}
    contract['tolerances'] = {case['output']['dtype']: {'kind': 'floating', **{k: case['correctness'][k] for k in ('atol', 'rtol')},
        'nan': 'same_positions', 'inf': 'same_sign_and_positions', 'comparison': 'elementwise_abs_rel'}}
    contract['baseline'] = {'program_id': task['id'] + '-baseline', 'artifact_digest': 'sha256:' + sha(task['seed_files']['solution.py'].encode())}
    contract['measurement_protocol']['rule_id'] = 'native-five-seed-12-pair-v1'
    for level in contract['measurement_protocol']['levels'].values():
        level['correctness_case_ids'] = ['case1']
        if level['timing']: level.update(blocks=12, samples_per_arm=20)
    contract['quality_policy']['rule_id'] = 'native-six-source-v1'
    return contract


def local_rsi(profile, output, request, kind='native'):
    cfg = {'kind': kind, 'state': str(output / 'evaluations'), 'soc': request['soc'], 'device': request['device']}
    if kind == 'sigmoid': cfg['suite'] = str(ROOT / 'examples/cann/sigmoid-optimization/suite')
    else: cfg.update(reference=request['reference'], operator=request['operator'], atol=request['atol'], rtol=request['rtol'])
    environment_names = ('PATH', 'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'ASCEND_HOME_PATH',
                         'ASCEND_OPP_PATH', 'ASCEND_AICPU_PATH', 'ASCEND_TOOLKIT_HOME',
                         'TBE_IMPL_PATH', 'PYTHONPATH', 'TMPDIR', 'CMAKE_PREFIX_PATH', 'ASC_DIR',
                         'LIBRARY_PATH', 'CPLUS_INCLUDE_PATH', 'C_INCLUDE_PATH')
    environment = {name: os.environ[name] for name in environment_names if name in os.environ}
    explicit = profile.get('npu_environment', {})
    if not isinstance(explicit, dict) or set(explicit) - set(environment_names) or any(
            not isinstance(value, str) or '\x00' in value for value in explicit.values()):
        raise ValueError('npu_environment must contain string values for supported CANN runtime variables')
    environment.update(explicit)
    paths = [profile['_python_path'], *filter(None, environment.get('PYTHONPATH', '').split(os.pathsep))]
    environment['PYTHONPATH'] = os.pathsep.join(dict.fromkeys(paths))
    environment.setdefault('PATH', os.defpath)
    npu_python = profile.get('npu_python', profile['python'])
    if not Path(npu_python).is_absolute() or not Path(npu_python).is_file():
        raise ValueError('npu_python must be an existing absolute Python executable')
    cfg.update(python=npu_python, python_real=str(Path(npu_python).resolve()), environment=environment)
    text = json.dumps(cfg, indent=2) + '\n'
    path = output / 'inputs' / profile.get('_config_name', 'rsi.json')
    python = Path(npu_python).resolve()
    pin_paths = [python, ROOT / 'examples/cann/rsi_local.py', ROOT / 'validation/native_rsi_adapter.py']
    if profile.get('_config_name') == 'diagnostics.json':
        pin_paths += sorted(p for p in (ROOT / 'examples/cann/tools').rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    venv_configuration = Path(npu_python).parent.parent / 'pyvenv.cfg'
    if venv_configuration.is_file():
        pin_paths.append(venv_configuration.resolve())
    if kind == 'sigmoid':
        pin_paths += [ROOT / 'validation/cannbench_sigmoid_adapter.py', *sorted(p for p in Path(cfg['suite']).rglob('*') if p.is_file())]
    pins = [{'path': str(p), 'sha256': sha(p.read_bytes())} for p in pin_paths]
    pins.append({'path': str(path), 'sha256': sha(text.encode())})
    if kind != 'sigmoid': pins.append({'path': request['reference'], 'sha256': sha(profile['_reference'].encode())})
    policy = {'seed': 17, 'slots': 5, 'exploration_fraction': 0.2, 'epsilon': 0.01, 'delta': 0.03, 'complexity_penalty': 0,
              'max_units': 1, 'minimum_pairs': 12, 'integration_interval': 16, 'holdout_limit': 40}
    return {'policy': policy, 'knowledge_query': None, 'footprints': [], 'task_category': 'cann-' + kind,
        'holdout_task': False, 'source_paths': ['solution.py'], 'credits': 10, 'timeout': 3600,
        'adapter_argv': [str(python), str(ROOT / 'examples/cann/rsi_local.py'), str(path)], 'adapter_files': pins,
        'replay_plugin': {'enabled': True, 'replay_on_compaction': True}}, text
