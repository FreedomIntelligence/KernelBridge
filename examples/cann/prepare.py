"""Freeze a CANN task and trusted profile before starting any model or NPU work."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
from urllib.parse import unquote, urlparse
from task_bundle import load_bundle
from goal_inputs import task_contract, local_rsi, native_runtime

BUNDLE = Path(__file__).resolve().parents[2]


def expand(value, variables):
    if isinstance(value, str):
        for key, replacement in variables.items():
            value = value.replace('{' + key + '}', replacement)
        if re.search(r'\{[a-z_]+\}', value):
            raise ValueError('unresolved profile placeholder: ' + value)
        return value
    if isinstance(value, list):
        return [expand(x, variables) for x in value]
    if isinstance(value, dict):
        for key in value:
            if key.lower() in ('api_key', 'password', 'token', 'authorization'):
                raise ValueError('credentials must be supplied through api_key_env, not the profile')
        return {k: expand(v, variables) for k, v in value.items()}
    return value


def prepare(task_path, profile_path, output, mode=None, session_seed=None, session_only=False, application_host=False):
    output = Path(output).absolute()
    if output.resolve() != output:
        raise ValueError('output must not traverse symlinks')
    for protected in (BUNDLE, Path(task_path).resolve().parent):
        if output == protected or output.is_relative_to(protected) or protected.is_relative_to(output):
            raise ValueError('output must be disjoint from source and task input directories')
    if output.exists():
        raise FileExistsError('preserve the original attempt; output already exists')
    raw = json.loads(Path(task_path).read_text())
    sigmoid = raw.get('contract', {}).get('suite') == 'CANNBench Season 1 Sigmoid, 20 cases'
    mode = mode or ('optimize' if sigmoid else raw.get('mode', 'generate'))
    if sigmoid:
        if mode != 'optimize': raise ValueError('Sigmoid reproduction uses Goal mode')
        compiled = raw
        reference = (BUNDLE / 'examples/cann/sigmoid-optimization/suite/reference.py').read_text()
        request = {'operator': 'sigmoid', 'soc': 'ai_core-Ascend910_9382', 'device': 1, 'fast': False, 'timeout': 780, 'atol': 1e-4, 'rtol': 1e-4}
        names = [Path(task_path).name, 'task-contract.json', 'baseline.py']
        inputs = {'schema_version': 'kernelbridge.cann-inputs/v1', 'mode': mode, 'source_directory': str(Path(task_path).parent),
                  'inputs': {name: {'sha256': hashlib.sha256((Path(task_path).parent / name).read_bytes()).hexdigest()} for name in names}}
    else:
        compiled, reference, request, inputs = load_bundle(task_path, mode)
    if application_host:
        compiled['contract']['acceptance'] = {'evidence_tier': 'E3' if mode == 'optimize' else 'E1'}
        measurement = {key: request[key] for key in ('soc', 'device', 'atol', 'rtol') if key in request}
        measurement.update(timer='npu_event', warmup=3, single_trials=100,
            pairing='alternating', paired_blocks=20 if sigmoid else 12,
            samples_per_arm=30 if sigmoid else 20, environment_source='original_device_receipt')
        measurement['version'] = hashlib.sha256(json.dumps(measurement, sort_keys=True).encode()).hexdigest()
        compiled['contract']['measurement_profile'] = measurement
    instruction = (BUNDLE / 'examples/cann/instructions/AGENTS.md').read_text()
    appendix = ('# Current operator task\n\n' + compiled['goal'] + '\n\n'
                + 'The frozen contract and reference files define inputs, precision and acceptance.\n'
                + '\n## Code generation workflow\n\n'
                + '1. Read the task, reference, initial implementation and existing receipts. Identify the required output and acceptance checks.\n'
                + '2. Check the configured device/compiler/runtime and the baseline evidence before attributing failures to a candidate.\n'
                + '3. State a short implementation plan. Make a coherent, small change with Pi file tools.\n'
                + '4. Compile and test through the active registered device tools. Read the original error, fix its cause, and rerun affected checks.\n'
                + '5. Use IR/profiler or knowledge tools when a concrete question calls for them.\n'
                + '6. Before finishing, match the delivered source hash to passing receipts. Any subsequent edit needs validation again.\n'
                + '7. Report the exact candidate, checks, result and remaining work. A turn limit is a stop reason, not successful completion.\n\n'
                + ('Use run_experiment for paired validation and unprofiled performance measurements. After rejection, read the restored source before editing again.\n'
                   if mode == 'optimize' else
                   'Use evaluate(candidate="solution.py") for native compilation, correctness and timing. Finish after a passing receipt for the final candidate.\n'))
    compiled['seed_files'].update({'AGENTS.md': instruction, 'AGENTS.task.md': appendix})
    profile = json.loads(Path(profile_path).read_text())
    allowed = {'schema_version', 'python', 'node', 'expected_python_modules', 'config', 'loopx', 'task_contract_file', 'npu_environment', 'npu_python', 'diagnostics', 'goal', 'checks', 'application', 'operator_infra', 'operator_pipeline', 'online_research'}
    if set(profile) - allowed or profile.get('schema_version') != 'kernelbridge.cann-profile/v1':
        raise ValueError('unsupported or unknown CANN profile field')
    profile = expand(profile, {'bundle': str(BUNDLE), 'run': str(output)})
    goal_policy = profile.get('goal', {})
    if not isinstance(goal_policy, dict) or set(goal_policy) - {'enabled', 'max_trials', 'complete_on_acceptance'}:
        raise ValueError('goal supports enabled, max_trials and complete_on_acceptance')
    use_goal = goal_policy.get('enabled', True) and not session_only
    max_trials = goal_policy.get('max_trials', 3)
    if type(use_goal) is not bool or type(max_trials) is not int or not 1 <= max_trials <= 100:
        raise ValueError('goal requires boolean enabled and max_trials in 1..100')
    if type(goal_policy.get('complete_on_acceptance', True)) is not bool:
        raise ValueError('complete_on_acceptance must be a boolean')
    if mode == 'optimize' and not use_goal:
        raise ValueError('Performance optimization uses LoopX Goal')
    for name in ('python', 'node', 'expected_python_modules'):
        if not Path(profile[name]).is_absolute() or not Path(profile[name]).is_file():
            raise ValueError(name + ' must select an existing absolute installed file')
    bindings = json.loads(Path(profile['expected_python_modules']).read_text())
    roots = {str(Path(unquote(urlparse(root).path)).parent)
             for module in bindings['modules'] for root in module['allowed_package_roots']}
    if len(roots) != 1:
        raise ValueError('expected one frozen installed Python module root')
    python_path = roots.pop()
    config = profile['config']
    config['application'] = dict(profile.get('application', {}), max_trials=max_trials,
        complete_on_acceptance=goal_policy.get('complete_on_acceptance', True))
    config.update(python_modules=bindings, workspace=str(output / 'workspace'),
                  state_dir=str(output / 'state'), writable_paths=['solution.py'], readonly_paths=['reference', 'AGENTS.md', 'AGENTS.task.md'])
    if session_seed is not None:
        session_seed = Path(session_seed).resolve()
        config['native_session_seed'] = {'path': str(session_seed), 'sha256': hashlib.sha256(session_seed.read_bytes()).hexdigest()}
        compiled['id'] = 'continued-' + uuid.uuid4().hex
        if not sigmoid:
            compiled['seed_files']['solution.py'] = compiled['seed_files']['reference/native-template.py']
    config.setdefault('pi_worker', {}).update(node=profile['node'], root=str(BUNDLE / 'source/apps/agent'))
    request.update(reference=str(output / 'inputs/reference.py'), jobs=str(output / 'evaluations'))
    request.pop('workspace', None); request.pop('candidate', None)
    contract = None
    rsi_text = None
    diagnostic_text = None
    if profile.get('diagnostics', False):
        from diagnostic_tools import tools as diagnostic_tools
        diagnostic_config, diagnostic_text = local_rsi({**profile, '_reference': reference, '_python_path': python_path,
            '_config_name': 'diagnostics.json'}, output, request)
        pins = diagnostic_config['adapter_files']
        config['business_tools'] = [*config.get('business_tools', []), *diagnostic_tools(
            [*diagnostic_config['adapter_argv'], '--diagnostics'], pins)]
    if mode == 'generate' and not use_goal:
        if config.get('rsi'):
            raise ValueError('generate requires an ordinary evaluator; use optimize for RSI')
        if not config.get('evaluator'):
            request['_runtime'] = native_runtime({**profile, '_python_path': python_path})
            config['evaluator'] = {'argv': [request['_runtime']['python_real'], str(BUNDLE / 'examples/cann/evaluate.py'), str(output / 'inputs/evaluation.json')],
                'parameters': {'type': 'object', 'properties': {'candidate': {'type': 'string'}}, 'required': ['candidate'], 'additionalProperties': False},
                'credits': 10, 'timeout': 1800, 'env_allowlist': []}
    elif mode == 'optimize':
        if config.get('evaluator'):
            raise ValueError('Performance goals use RSI and registered check tools')
        if not config.get('rsi'):
            config['rsi'], rsi_text = local_rsi({**profile, '_reference': reference, '_python_path': python_path}, output, request, 'sigmoid' if sigmoid else 'native')
        else:
            rsi_text = None
    if use_goal:
        if not application_host and (not profile.get('loopx') or not Path(profile['loopx']).is_file()):
            raise ValueError('Goal mode needs the installed loopx executable')
        if mode == 'generate' and (config.get('evaluator') or config.get('rsi')):
            raise ValueError('Functional goals use operator_evaluate as a business tool')
        if not sigmoid and profile.get('checks', True):
            from evaluation_tools import configure
            baseline = compiled['seed_files']['reference/native-template.py']
            request['_baseline'] = str(output / 'inputs/baseline.py')
            selected_tools = {tool['name'] for tool in config.get('business_tools', [])}
            required_tools = {'baseline_check', 'operator_check'} | ({'operator_evaluate'} if mode == 'generate' else set())
            if not required_tools <= selected_tools:
                request['_runtime'] = native_runtime({**profile, '_python_path': python_path})
            configure(config, output, request, reference, baseline, functional=mode == 'generate')
            appendix += '\nBefore editing or evaluating a candidate, call baseline_check(). Use operator_check() for compile/correctness feedback before longer measurements.\n'
        if mode == 'generate':
            if not config.get('candidate_retention'):
                raise ValueError('Functional Goal requires source-bound operator_evaluate retention')
            compiled['goal'] = compiled['goal'].replace('evaluate(candidate="solution.py")', 'operator_evaluate()')
            appendix = appendix.replace('evaluate(candidate="solution.py")', 'operator_evaluate()')
        compiled['seed_files']['AGENTS.task.md'] = appendix
        if profile.get('task_contract_file'):
            contract = json.loads(Path(profile['task_contract_file']).read_text())
        elif sigmoid:
            contract = json.loads((Path(task_path).parent / 'task-contract.json').read_text())
        else:
            contract = task_contract(compiled, reference)
        identity_reference = (BUNDLE / 'examples/cann/sigmoid-optimization/suite/golden.py').read_bytes() if sigmoid else reference.encode()
        if contract['operator']['reference_digest'] != 'sha256:' + hashlib.sha256(identity_reference).hexdigest():
            raise ValueError('Goal contract reference digest must match the frozen case/reference')
    env = {**os.environ, 'PYTHONPATH': python_path, 'PYTHONDONTWRITEBYTECODE': '1'}
    check = subprocess.run([profile['python'], '-B', '-c',
        'import json,sys; from kernelcat_worker.configuration import Config; from kernelcat_executor.contracts.models import Task; v=json.load(sys.stdin); Config.model_validate_json(json.dumps(v["config"])); Task.model_validate_json(json.dumps(v["task"]))'],
        input=json.dumps({'config': config, 'task': compiled}), text=True, capture_output=True, env=env, timeout=30)
    if check.returncode:
        raise ValueError('installed worker rejected frozen config/task: ' + check.stderr[-3000:])
    output.mkdir(mode=0o700)
    (output / 'inputs').mkdir()
    (output / 'inputs/source').mkdir()
    if (profile.get('operator_infra') or profile.get('operator_pipeline')
            or ('online_research' in profile and profile['online_research'] is not False)):
        import sys
        sys.path.insert(0, str(BUNDLE / 'examples'))
        from operator_tools import prepare as prepare_operator_tools
        config['business_tools'] = prepare_operator_tools({**profile, 'business_tools': config.get('business_tools', [])}, output / 'inputs', profile['python'])
    def save(name, value):
        with (output / name).open('x') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2); stream.write('\n')
    save('task.json', compiled); save('config.json', config); save('config.redacted.json', config)
    save('workflow.json', {'mode': mode, 'goal': use_goal, 'max_trials': max_trials,
        'complete_on_acceptance': goal_policy.get('complete_on_acceptance', True)})
    save('input-manifest.json', inputs); save('inputs/evaluation.json', request)
    save('instruction-manifest.json', {'loader': 'Pi DefaultResourceLoader.agentsFilesOverride',
        'files': {name: {'sha256': hashlib.sha256(text.encode()).hexdigest(), 'bytes': len(text.encode())}
                  for name, text in [('AGENTS.md', instruction), ('AGENTS.task.md', appendix)]}})
    (output / 'inputs/reference.py').write_text(reference)
    if not sigmoid:
        (output / 'inputs/baseline.py').write_text(compiled['seed_files']['reference/native-template.py'])
    for name in inputs['inputs']:
        (output / 'inputs/source' / name).write_bytes((Path(task_path).parent / name).read_bytes())
    command = {'argv': [profile['node'], str(BUNDLE / 'source/apps/agent/src/main.ts'), 'run', '--python', profile['python'],
        '--expected-python-modules', profile['expected_python_modules'], '--config', str(output / 'config.json'),
        '--task', str(output / 'task.json'), '--timeout-ms', '7200000'],
        'python_path': python_path, 'api_key_env': config['providers']['primary']['api_key_env']}
    save('command.json', command)
    if contract:
        save('task-contract.json', contract)
        check_contract = subprocess.run([profile['node'], '--input-type=module', '-e', 'import {validateTaskContract} from ' + json.dumps((BUNDLE / 'source/apps/agent/src/tasks/contract.ts').as_uri()) + '; import fs from \"node:fs\"; validateTaskContract(JSON.parse(fs.readFileSync(process.argv[1], \"utf8\")));', str(output / 'task-contract.json')], capture_output=True, text=True, timeout=30)
        if check_contract.returncode: raise ValueError(check_contract.stderr)
        if rsi_text: (output / 'inputs/rsi.json').write_text(rsi_text)
    if diagnostic_text:
        (output / 'inputs/diagnostics.json').write_text(diagnostic_text)
    save('prepared.json', {'mode': mode, 'profile': str(Path(profile_path).resolve()), 'model_started': False, 'device_started': False})
    return {'output': str(output), 'bundle': str(BUNDLE), 'python': profile['python'],
            'expectedPythonModules': profile['expected_python_modules'], 'pythonPath': python_path,
            'loopx': profile.get('loopx'), 'mode': mode,
            'goal': use_goal,
            'localRsi': str(output / 'inputs/rsi.json') if mode == 'optimize' and rsi_text else None}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('task', 'profile', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--mode', choices=['generate', 'optimize'])
    parser.add_argument('--session-only', action='store_true')
    parser.add_argument('--application-host', action='store_true')
    parser.add_argument('--session-seed', type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.task, args.profile, args.output, args.mode, args.session_seed, args.session_only, args.application_host)))
