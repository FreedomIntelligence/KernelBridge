"""Operator evaluation tools and their source-bound acceptance policy."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def outcome_schema(reference, request, *, timing=False, candidate=None):
    native = {'type': 'object', 'required': ['compiled', 'correctness', 'failure_stage', 'hardware', 'correctness_policy', 'build'],
        'properties': {'compiled': {'const': True}, 'correctness': {'const': True}, 'failure_stage': {'const': None},
            'hardware': {'const': request['soc']}, 'correctness_policy': {'const': {k: request[k] for k in ('atol', 'rtol')}},
            'build': {'type': 'object', 'required': ['files'], 'properties': {'files': {'type': 'array', 'minItems': 1}}}}}
    if timing:
        native['required'].append('performance')
        native['properties']['performance'] = {'type': 'object', 'required': ['timer', 'num_trials', 'samples_ms'],
            'properties': {'timer': {'const': 'npu_event'}, 'num_trials': {'const': 100},
                'samples_ms': {'type': 'array', 'minItems': 100, 'maxItems': 100, 'items': {'type': 'number', 'exclusiveMinimum': 0}}}}
    data = {'type': 'object', 'required': ['candidate_sha256', 'reference_sha256', 'result'], 'properties': {
        'candidate_sha256': {'const': candidate} if candidate else {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
        'reference_sha256': {'const': hashlib.sha256(reference.encode()).hexdigest()}, 'result': native}}
    return {'type': 'object', 'required': ['status', 'data'], 'properties': {'status': {'const': 'completed'}, 'data': data}}


def configure(config, output, request, reference, baseline, *, functional):
    """A deployment may supply the same tool contracts using a remote transport."""
    existing = {tool['name'] for tool in config.get('business_tools', [])}
    specs = [('baseline_check', 'Compile and check the frozen initial implementation on the configured device.', False),
             ('operator_check', 'Compile and check solution.py against all task inputs, without repeated performance sampling.', True)]
    if functional:
        specs.append(('operator_evaluate', 'Compile, check and time the final solution.py. A passing source-bound receipt qualifies delivery.', True))
    for name, capture in [(name, capture) for name, _, capture in specs if name in existing]:
        for tool in config['business_tools']:
            if tool['name'] == name and capture:
                tool['capture_paths'] = ['solution.py']
    missing = [spec for spec in specs if spec[0] not in existing]
    if missing:
        runtime = request['_runtime']
        pins = [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in [Path(runtime['python_real']), ROOT / 'examples/cann/evaluate.py']]
        pins += [{'path': str(output / 'inputs/evaluation.json'), 'sha256': hashlib.sha256((json.dumps(request, ensure_ascii=False, indent=2) + '\n').encode()).hexdigest()},
                 {'path': str(output / 'inputs/reference.py'), 'sha256': hashlib.sha256(reference.encode()).hexdigest()},
                 {'path': str(output / 'inputs/baseline.py'), 'sha256': hashlib.sha256(baseline.encode()).hexdigest()}]
        config.setdefault('business_tools', []).extend({'name': name, 'description': description,
            'argv': [runtime['python_real'], str(ROOT / 'examples/cann/evaluate.py'), str(output / 'inputs/evaluation.json')],
            'files': pins, 'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
            'capture_paths': ['solution.py'] if capture else [], 'credits': 10, 'timeout': 1800}
            for name, description, capture in missing)
    config['required_checks'] = [{'tool': 'baseline_check',
        'before_tools': ['write', 'edit', 'write_file', 'edit_file', 'bash', 'run_experiment', 'operator_check', 'operator_evaluate', 'ir_collect', 'profiler_collect'],
        'outcome_schema': outcome_schema(reference, request, candidate=hashlib.sha256(baseline.encode()).hexdigest())}]
    if functional:
        config['candidate_retention'] = {'tool': 'operator_evaluate', 'source_hashes': {'solution.py': 'data.candidate_sha256'},
            'outcome_schema': outcome_schema(reference, request, timing=True)}
