"""A-J adapter over one original NPU job: E executes, F/G/J read its receipts."""
import base64
import hashlib
import json
from pathlib import Path
import sys

from npu_trial import invoke_remote, save, transport, trial


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest_sha(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def verify(source):
    if source['sha256'] != manifest_sha(source['files']): raise ValueError('Frozen manifest changed')
    for name, expected in source['files'].items():
        path = Path(source['root']) / name
        if Path(name).is_absolute() or '..' in Path(name).parts or path.resolve() != path or sha(path) != expected:
            raise ValueError('Frozen source changed or escaped its root')


def round_for(config, request):
    expected = request['binding']['source']
    matches = []
    for path in Path(config['state']).glob('round-*/intent.json'):
        intent = json.loads(path.read_text())
        if manifest_sha({'candidate.py': intent['source_sha256']}) == expected:
            matches.append((path.parent, intent))
    if len(matches) != 1: raise ValueError('No unique original NPU attempt for this frozen candidate')
    return matches[0]


def original(config, request):
    folder, intent = round_for(config, request)
    result = json.loads((folder / 'device-result.json').read_text())
    summary = json.loads((folder / 'result.json').read_text())
    observation = json.loads((folder / 'observation.json').read_text())
    original_bytes = (json.dumps(observation['result'], indent=2, allow_nan=False) + '\n').encode()
    original_sha = hashlib.sha256(original_bytes).hexdigest()
    if original_sha != observation['result_sha256'] or original_sha != summary['remote_result_sha256']:
        raise ValueError('Original device result hash changed')
    if result['source_sha256'] != intent['source_sha256'] or sha(folder / 'candidate.py') != intent['source_sha256']:
        raise ValueError('Original candidate identity changed')
    if request.get('handle') and request['handle'] != summary['original_handle']:
        raise ValueError('Requested handle differs from the original job')
    oracle = request['contract']['oracle']['files']; verify(oracle)
    if result['driver_sha256'] != oracle['files'].get('npu_benchmark.py') or result['baseline_sha256'] != oracle['files'].get('npu_eager.py'):
        raise ValueError('Original benchmark or eager source differs from the pinned oracle contract')
    measurement = request['contract']['measurement']
    for key in ('device', 'warmup', 'repeats', 'pairs'):
        if measurement[key] != result[key]: raise ValueError('Measurement contract differs: ' + key)
    evidence = {'round': intent['round'], 'native_candidate_sha256': intent['source_sha256'],
                'remote_result_sha256': original_sha, 'original_handle': summary['original_handle'],
                'original_job': summary['remote_attempt'].rstrip('/') + '/job',
                'read_only_projection': True, 'scope': 'same_device_npu_eager_abba'}
    return folder, result, summary, evidence


READ_ARTIFACT = r'''import base64,hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve(); path=(root/sys.argv[2]).resolve()
if not path.is_relative_to(root) or not path.is_file() or not 0<path.stat().st_size<=8*1024*1024:raise ValueError('Original artifact is missing or outside bounded job')
data=path.read_bytes();print(json.dumps({'sha256':hashlib.sha256(data).hexdigest(),'data':base64.b64encode(data).decode()}))
'''


def diagnostic(config, request, kind, result, evidence):
    found = [row for row in (result.get('diagnostics') or {}).get('reports', []) if row.get('kind') == kind]
    if len(found) != 1:
        return {'kind': kind, 'status': 'unavailable', 'reason': 'Original job did not collect this diagnostic; no replacement job was launched', **evidence}
    report = found[0]
    paths = report.get('artifacts', {}) if report.get('status') == 'completed' else report.get('attempt_evidence', {})
    local = Path(request['output']) / 'artifacts'; local.mkdir()
    copied = {}
    for index, (relative, expected) in enumerate(sorted(paths.items())[:2]):
        response = invoke_remote(transport(config), READ_ARTIFACT, evidence['original_job'], relative)
        data = base64.b64decode(response['data'])
        if response['sha256'] != expected or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('Original diagnostic artifact hash changed')
        name = f'{kind}-{index}' + Path(relative).suffix
        (local / name).write_bytes(data); copied['artifacts/' + name] = expected
    return {'kind': kind, 'status': report.get('status', 'completed'), 'reason': report.get('reason'),
            **evidence, 'scope': 'npu_diagnostic', 'candidate_sha256': request['source']['sha256'],
            'artifacts': copied, 'original_artifacts': paths}


def execute(config, operation, request):
    verify(request['source'])
    if operation == 'launch':
        if set(request['source']['files']) != {'candidate.py'}: raise ValueError('Freeze exactly candidate.py')
        number = json.loads((Path(config['state']) / 'requested.json').read_text())['round']
        source_sha = request['source']['files']['candidate.py']
        stage = Path(config['state']).parent / 'trial-inputs' / f'{number:02d}-{source_sha[:16]}'
        stage.mkdir(parents=True, exist_ok=True)
        candidate = stage / 'candidate.py'
        if candidate.exists() and sha(candidate) != source_sha: raise ValueError('Staged frozen source changed')
        if not candidate.exists(): candidate.write_bytes((Path(request['source']['root']) / 'candidate.py').read_bytes())
        events = sorted(Path(config['pipeline_state']).glob('event-*.json'))
        hypothesis = json.loads(events[-1].read_text())['body'].get('C') if events else None
        if not hypothesis: raise ValueError('Record pipeline_plan C before launching')
        outcome = trial(dict(config, candidate=str(candidate)), {'id': f'npu-pipeline-{number}-{source_sha}',
                        'arguments': {'round': number, 'hypothesis': json.dumps(hypothesis, ensure_ascii=False)}})
        if outcome['status'] != 'completed': raise RuntimeError('Original NPU job unresolved; recover this launch with npu-collect')
        save(Path(config['candidate']).parent / 'feedback.json', outcome['data'])
    elif operation == 'collect':
        folder, intent = round_for(config, request)
        if not (folder / 'result.json').exists():
            outcome = trial(config, recover=intent['round'])
            if outcome['status'] != 'completed': raise RuntimeError('Original NPU job is still unresolved')
    folder, result, summary, evidence = original(config, request)
    if operation in ('launch', 'collect'):
        return {'status': 'completed', 'handle': summary['original_handle'], 'job_status': result['status'],
                'execution': 'E executed the complete native job once; F/G/J only project this original receipt', **evidence}
    cases = result.get('cases', [])
    expected = sorted(case['id'] for case in request['contract']['suite'])
    correct = result.get('correct') is True and summary.get('correct') is True and sorted(case['id'] for case in cases) == expected
    if operation == 'check':
        return {'checks': {'build': result['status'] == 'completed', 'semantics': correct, 'full': correct},
                'case_count': len(cases), 'case_ids': [case['id'] for case in cases],
                'oracle_sha256': request['contract']['oracle']['files']['sha256'], 'error': result.get('message'), **evidence}
    if operation == 'measure':
        arm = int(Path(request['output']).name.removeprefix('arm-'))
        if arm not in range(4) or request['role'] != 'ABBA'[arm]: raise ValueError('Expected one original ABBA arm index')
        source_files = request['source']['files']
        if request['role'] == 'A':
            if source_files != {'npu_eager.py': result['baseline_sha256']}: raise ValueError('Baseline differs from original eager source')
        elif source_files != {'candidate.py': result['source_sha256']}:
            raise ValueError('Candidate differs from original paired source')
        samples = {}
        for case in cases:
            original_arms = case['arms']
            if len(original_arms) != 4 * result['pairs']: raise ValueError('Original ABBA samples are incomplete')
            for index, row in enumerate(original_arms):
                if row['pair'] != index // 4 or row['arm'] != ('eager', 'candidate', 'candidate', 'eager')[index % 4]:
                    raise ValueError('Original ABBA arm order changed')
            samples[case['id']] = [original_arms[pair * 4 + arm]['us'] for pair in range(result['pairs'])]
        return {'correct': correct, 'cases': samples, 'measurement': request['contract']['measurement'],
                'environment': dict(result['environment'], device_index=result['device'], soc=result['soc'],
                                    original_result_sha256=evidence['remote_result_sha256']),
                'original_arm_index': arm, 'new_measurements': 0, **evidence}
    if operation in ('ir', 'profiler'):
        return diagnostic(config, request, operation, result, evidence)
    raise ValueError('Unknown adapter operation')


if __name__ == '__main__':
    config = json.loads(Path(sys.argv[1]).read_text())
    print(json.dumps(execute(config, sys.argv[2], json.load(sys.stdin)), ensure_ascii=False, allow_nan=False))
