"""Prepare an empty operator workspace and source-bound A-J tools; never call a model or NPU.

Candidate implementation is deliberately absent. The application's model-turn
limit is independent from the number of native attempts made during the trial.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import uuid

HERE = Path(__file__).resolve().parent
NATIVE_INTERFACE = HERE.parent / 'cann/native-interface.md'
NATIVE_INTERFACE_VERSION = 'kernelbridge.native-six-source-interface/v2'


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    path.chmod(0o600)


def scatter_contract(mode, device):
    cases = [(1, 1, 'sparse'), (17, 7, 'sparse'), (129, 65, 'uniform'),
             (256, 128, 'collision'), (33, 257, 'sorted'), (513, 32, 'hot')]
    return {
        'task': {'operator': 'feature-index scatter ' + mode,
                 'inputs': 'finite FP32 contiguous src[N,F], int64 index[N], 0 <= index < 33',
                 'outputs': 'FP32 [33,F], dim=0 reduction, values only, empty bins zero'},
        'oracle': {'kind': 'independent', 'description': 'Independent CPU per-bin amax/amin, excluded from timing'},
        'suite': [{'id': f'{family}-{n}x{f}', 'rows': n, 'features': f, 'family': family} for n, f, family in cases],
        'measurement': {'scope': 'same_device_npu_eager_abba', 'device': device, 'warmup': 3, 'repeats': 10,
                        'pairs': 3, 'aggregation': 'pooled_arm_median', 'require_noise': True,
                        'protocol_id': 'scatter-fp32-index-extrema-eager-abba-3x10-v1', 'allocation': 'both APIs allocate output', 'unit': 'us', 'timer': 'synchronized_host_clock'},
        'qualification': 'Original same-device eager ABBA. Across-attempt ranking is best observed, not a paired incumbent promotion or official result.',
    }


def prepare(args):
    supplied = json.loads(args.profile.read_text()) if args.profile else {}
    diagnostic_switches = supplied.get('diagnostics', {})
    if (not isinstance(diagnostic_switches, dict)
            or set(diagnostic_switches) - {'on_failure', 'on_regression', 'required'}
            or any(type(value) is not bool for value in diagnostic_switches.values())):
        raise ValueError('diagnostics accepts only boolean on_failure, on_regression and required switches')
    mode = supplied.get('mode', args.mode)
    if not mode: raise ValueError('NPU profile or --mode must select the declared task')
    device = supplied.get('device', args.device)
    if type(device) is not int or device < 0: raise ValueError('An explicit NPU device is required')
    transport = Path(supplied.get('transport_module') or args.transport).resolve(strict=True)
    remote_root = supplied.get('remote_root', args.remote_root)
    if not remote_root or not remote_root.startswith('/'): raise ValueError('An absolute remote task root is required')
    task = json.loads(args.task.read_text()) if args.task else {
        'schema_version': 'kernelbridge.clean-npu-task/v1',
        'goal': '实现并验证特征索引' + ('最大值' if mode == 'max' else '最小值') + '算子，通过全部固定 case，并取得相对同设备 eager NPU 高于校准噪声的正向配对加速和晋升。',
        'contract': scatter_contract(mode, device),
        'candidate_abi': {'native_operator': 'scatter_' + mode, 'operator_type': 'Scatter' + mode.capitalize() + 'Custom',
                          'model': 'ModelNew.forward(src, index)', 'binding': 'custom_ops_lib.run(src, index)'},
    }
    if not (task.get('instruction') or task.get('goal')) or not task.get('contract') or any(key in task for key in ('seed_files', 'candidate', 'initial_candidate', 'baseline_file')):
        raise ValueError('Clean task requires goal/contract and must not supply an implementation or seeded files')
    contract = task['contract']
    oracle_files = supplied.get('oracle_files', {'npu_benchmark.py': str(HERE / 'npu_benchmark.py'), 'npu_eager.py': str(HERE / 'npu_eager.py')})
    baseline_files = supplied.get('baseline_files', {'npu_eager.py': str(HERE / 'npu_eager.py')})
    oracle_files = {name: str(Path(path).resolve(strict=True)) for name, path in oracle_files.items()}
    baseline_files = {name: str(Path(path).resolve(strict=True)) for name, path in baseline_files.items()}
    contract['oracle'] = dict(contract['oracle'], files=oracle_files)
    measurement = contract['measurement']
    if measurement['device'] != device: raise ValueError('Task/profile device differs')
    if mode in ('max', 'min') and measurement != scatter_contract(mode, device)['measurement']:
        raise ValueError('Scatter six-case measurement contract is fixed')
    if mode in ('max', 'min') and contract['suite'] != scatter_contract(mode, device)['suite']:
        raise ValueError('Scatter six-case suite is fixed')
    root = args.output.resolve(); root.mkdir(parents=True, exist_ok=False)
    project = root / 'project'; project.mkdir()
    state = root / 'attempts'; state.mkdir()
    candidate = project / 'candidate.py'  # Path only: the model must create this file.
    save(project / 'contract.json', contract)
    trial_id = 'trial-' + uuid.uuid4().hex
    config = {'mode': mode, 'device': device, 'candidate': str(candidate), 'state': str(state), 'contract': contract,
              'transport_module': str(transport), 'remote_root': remote_root, 'attempt_namespace': trial_id,
              'benchmark_script': supplied.get('benchmark_script', 'npu_benchmark.py'),
              'oracle_driver_file': supplied.get('oracle_driver_file', 'npu_benchmark.py'),
              'baseline_file': supplied.get('baseline_file', 'npu_eager.py'),
              'warmup': measurement['warmup'], 'repeats': measurement['repeats'], 'pairs': measurement['pairs'],
              'wait_seconds': 900, 'poll_seconds': 5, 'profile_attempts': [1], 'pipeline_state': str(root / 'operator-pipeline')}
    config_path = root / 'trial-config.json'; save(config_path, config)
    names = {'launch': 'npu-launch', 'collect': 'npu-collect', 'check': 'npu-check',
             'measure': 'npu-measure', 'ir': 'npu-ir', 'profiler': 'npu-profiler'}
    save(project / 'workflow-inputs.json', {'contract': contract,
        'baseline': {'selection': 'files', 'files': baseline_files}, 'freeze': {'files': {'candidate.py': 'candidate.py'}},
        'drivers': names, 'candidate_abi': task.get('candidate_abi'),
        'execution': 'E executes one native job containing build, correctness and ABBA; F/G/J only project its original receipt.',
        'calibration': 'unavailable: no official calibration driver; I may archive accepted local evidence only'})
    interface = NATIVE_INTERFACE.read_text()
    (project / 'ABI.md').write_text(
        '# Task ABI\n\nDeclared by this task; the shared compiler interface follows.\n\n```json\n'
        + json.dumps(task.get('candidate_abi') or {}, ensure_ascii=False, indent=2)
        + '\n```\n\n' + interface)
    authored = {'schema_version': 'kernelbridge.operator-task/v1', 'instruction': task.get('instruction', task.get('goal')),
                'contract': contract, 'files': {path.name: path.read_text() for path in sorted(project.iterdir())},
                'writable': ['candidate.py'], 'readonly': sorted(path.name for path in project.iterdir())}
    save(root / 'task.json', authored)
    pins = sorted(HERE.glob('npu_*.py')) + [HERE.parent / 'operator_pipeline/diagnostic_policy.py',
            HERE.parent / 'operator_pipeline/job_identity.py', HERE.parent / 'operator_pipeline/preflight.py', config_path,
            root / 'task.json', project / 'contract.json', transport]
    profile = {'candidate_source': 'workspace', 'read_roots': [str(root), *sorted(set([*oracle_files.values(), *baseline_files.values()]))],
        'drivers': {name: {'kind': 'diagnostic' if operation in ('ir', 'profiler') else operation,
                    'argv': [str(args.python.absolute()), '-B', str(HERE / 'npu_pipeline_driver.py'), str(config_path), operation],
                    'files': [str(path) for path in pins], 'timeout': 1800 if operation in ('launch', 'collect') else 180,
                    **({'preflight_rejection_protocol': 'kernelbridge.driver-preflight/v1'} if operation == 'launch' else {})}
                    for operation, name in names.items()},
        'diagnostics': {'on_failure': True, 'on_regression': True, 'required': False, **diagnostic_switches,
                        'drivers': {'ir': names['ir'], 'profiler': names['profiler']}, 'scope': 'npu_diagnostic'}}
    save(root / 'workflow-profile.json', profile)
    spec = importlib.util.spec_from_file_location('npu_workflow_prepare', HERE.parent / 'operator_pipeline/prepare.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    tools = module.prepare(profile, root / 'pipeline-tools', args.python)
    for tool in tools:
        if tool['name'] in {'pipeline_freeze', 'pipeline_evaluate'}: tool['capture_paths'] = ['candidate.py']
        if tool['name'] == 'pipeline_pair':
            tool['description'] = 'G: project original E job ABBA samples into four original arm receipts; no new measurement. Inspect full-case speedups. J exposes original source-bound diagnostics for investigating failures and revising hypotheses.'
        elif tool['name'] == 'pipeline_run':
            tool['description'] = 'E npu-launch executes one frozen NPU job; npu-collect observes the original handle. F npu-check validates its original full-suite receipt. J npu-ir/npu-profiler recover actual source-bound diagnostic files. Unknown never authorizes a replacement launch.'
    save(root / 'business-tools.json', tools)
    fragment = {'schema_version': 'kernelbridge.operator-profile/v1', 'config': {'business_tools': tools},
                'acceptance': {'scope': measurement['scope'], 'baseline_files': baseline_files},
                'application': {'max_model_turns': 20}, 'goal': {'enabled': True, 'max_trials': 1, 'complete_on_acceptance': True}}
    save(root / 'profile-fragment.json', fragment)
    if getattr(args, 'runtime_profile', None):
        runtime = json.loads(args.runtime_profile.read_text())
        runtime.update(schema_version=fragment['schema_version'], goal=fragment['goal'], acceptance=fragment['acceptance'])
        runtime['config'] = dict(runtime.get('config', {}), business_tools=tools)
        runtime['application'] = dict(runtime.get('application', {}), max_model_turns=20)
        save(root / 'profile.json', runtime)
    save(root / 'clean-input-manifest.json', {'trial_id': trial_id, 'candidate_present': False,
         'session_history': None, 'seed_implementation': None, 'formal_device_jobs': 0,
         'application': {'max_model_turns': 20}, 'task_source': str(args.task.resolve()) if args.task else None,
         'npu_profile_source': str(args.profile.resolve()) if args.profile else None,
         'native_interface': {'version': NATIVE_INTERFACE_VERSION, 'source': 'examples/cann/native-interface.md',
                              'sha256': hashlib.sha256(interface.encode()).hexdigest()}})
    return {'trial': str(root), 'project': str(project), 'task': str(root / 'task.json'),
            'tools': str(root / 'business-tools.json'), 'mode': mode, 'device': device, 'candidate_present': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--task', type=Path)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--runtime-profile', type=Path)
    parser.add_argument('--mode', choices=['max', 'min'])
    parser.add_argument('--device', type=int)
    parser.add_argument('--transport', type=Path)
    parser.add_argument('--remote-root')
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    print(json.dumps(prepare(parser.parse_args())))
