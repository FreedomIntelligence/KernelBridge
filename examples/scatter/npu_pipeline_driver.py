"""A-J adapter over one original NPU job: E executes, F/G/J read its receipts."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from npu_trial import LOG_PAGE_BYTES, MAX_LOG_BYTES, attempt_records, attempt_number, invoke_remote, recover_run_log, save, transport, trial, same_handle, wire_handle, wire_summary
from npu_diagnostics import MAX_ARTIFACT
try:
    from preflight import PROTOCOL, contract_differences, digest as preflight_digest, make_rejection
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'operator_pipeline'))
    from preflight import PROTOCOL, contract_differences, digest as preflight_digest, make_rejection


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest_sha(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class ContractMismatch(ValueError):
    """Only a strict, source-verified contract comparison may emit rejection."""
    def __init__(self, expected, actual):
        super().__init__('Declared semantic, suite, oracle or measurement contract differs from the frozen task')
        self.expected = expected
        self.actual = actual


def write_preflight_artifact(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def verify(source):
    if source['sha256'] != manifest_sha(source['files']): raise ValueError('Frozen manifest changed')
    for name, expected in source['files'].items():
        path = Path(source['root']) / name
        if Path(name).is_absolute() or '..' in Path(name).parts or path.resolve() != path or sha(path) != expected:
            raise ValueError('Frozen source changed or escaped its root')


def verify_contract(config, request):
    expected = json.loads(json.dumps(config['contract'], allow_nan=False))
    declared = json.loads(json.dumps(request['contract'], allow_nan=False))
    oracle = declared['oracle']['files']; verify(oracle)
    declared['oracle']['files'] = oracle['files']
    expected['oracle']['files'] = {name: sha(path) for name, path in expected['oracle']['files'].items()}
    if contract_differences(expected, declared):
        raise ContractMismatch(expected, declared)


def round_for(config, request):
    expected = request['binding']['source']
    matches = []
    for path in attempt_records(Path(config['state'])):
        intent = json.loads(path.read_text())
        if manifest_sha({'candidate.py': intent['source_sha256']}) == expected:
            matches.append((path.parent, intent))
    if len(matches) != 1: raise ValueError('No unique original NPU attempt for this frozen candidate')
    return matches[0]


def original(config, request):
    folder, intent = round_for(config, request)
    result = json.loads((folder / 'device-result.json').read_text())
    summary = wire_summary(json.loads((folder / 'result.json').read_text()))
    observation = json.loads((folder / 'observation.json').read_text())
    original_bytes = (json.dumps(observation['result'], indent=2, allow_nan=False) + '\n').encode()
    original_sha = hashlib.sha256(original_bytes).hexdigest()
    if original_sha != observation['result_sha256'] or original_sha != summary['remote_result_sha256']:
        raise ValueError('Original device result hash changed')
    qualified = observation['result']
    if observation.get('cpu_fallback') or any(marker in observation.get('log_tail', '') for marker in ('fall back to run on the CPU', 'npu_cpu_fallback')):
        qualified = {**qualified, 'status': 'failed', 'correct': False, 'message': 'CPU fallback detected; not qualified NPU timing'}
    if result != qualified or summary['status'] != result['status'] or summary['correct'] != result.get('correct', False):
        raise ValueError('Qualified local result differs from the original device receipt')
    if result['source_sha256'] != intent['source_sha256'] or sha(folder / 'candidate.py') != intent['source_sha256']:
        raise ValueError('Original candidate identity changed')
    if request.get('handle') and not same_handle(request['handle'], summary['original_handle']):
        raise ValueError('Requested handle differs from the original job')
    oracle = request['contract']['oracle']['files']; verify(oracle)
    if result['driver_sha256'] != oracle['files'].get(config.get('oracle_driver_file', 'npu_benchmark.py')) or result['baseline_sha256'] != oracle['files'].get(config.get('baseline_file', 'npu_eager.py')):
        raise ValueError('Original benchmark or eager source differs from the pinned oracle contract')
    measurement = request['contract']['measurement']
    for key in ('device', 'warmup', 'repeats', 'pairs'):
        if measurement[key] != result[key]: raise ValueError('Measurement contract differs: ' + key)
    evidence = {'attempt': attempt_number(intent), 'native_candidate_sha256': intent['source_sha256'],
                'remote_result_sha256': original_sha, 'original_handle': summary['original_handle'],
                'original_job': summary['remote_attempt'].rstrip('/') + '/job',
                'read_only_projection': True, 'scope': 'same_device_npu_eager_abba'}
    return folder, result, summary, evidence


def verify_log_binding(record, evidence):
    if (record.get('source_sha256') != evidence['native_candidate_sha256']
            or record.get('remote_result_sha256') != evidence['remote_result_sha256']
            or record.get('remote_attempt', '').rstrip('/') + '/job' != evidence['original_job']
            or not same_handle(record.get('original_handle'), evidence['original_handle'])):
        raise ValueError('Original run log binding changed')


def terminal_feedback(folder, request, result, evidence):
    """Project already verified terminal evidence; never execute or repair work."""
    if result.get('status') not in {'completed', 'failed'}:
        raise ValueError('Original evaluation has no terminal result; collect its existing handle')
    output = Path(request['output']) / 'evaluation'; output.mkdir(parents=True, exist_ok=True)
    observation = json.loads((folder / 'observation.json').read_text())
    original_bytes = (json.dumps(observation['result'], indent=2, allow_nan=False) + '\n').encode()
    if hashlib.sha256(original_bytes).hexdigest() != evidence['remote_result_sha256']:
        raise ValueError('Original evaluation changed before projection')
    action_id = request.get('action_id')
    run = hashlib.sha256(action_id.encode()).hexdigest() if isinstance(action_id, str) and action_id else None
    artifacts = {}

    def retain(name, data):
        path = output / name; path.write_bytes(data)
        relative = 'evaluation/' + name
        item = {'path': relative, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
        if run:
            item['read'] = {'tool': 'pipeline_read', 'arguments': {'run': run, 'path': 'driver/' + relative}}
        artifacts[name] = item

    retain('original-result.json', original_bytes)
    # CPU-fallback qualification may differ from the raw original. Retain both.
    retain('qualified-result.json', (json.dumps(result, indent=2, allow_nan=False) + '\n').encode())
    texts = []
    log = {'status': 'unavailable', 'complete': False,
           'reason': 'Legacy observation has no retained full log; no remote job was contacted'}
    log_receipt = folder / 'run-log.json'
    if log_receipt.exists():
        record = json.loads(log_receipt.read_text())
        verify_log_binding(record, evidence)
        retain('run-log.json', (json.dumps(record, indent=2, allow_nan=False) + '\n').encode())
        log = {key: record[key] for key in ('status', 'complete', 'bytes', 'sha256', 'reason', 'recovery', 'recovery_error') if key in record}
        log['receipt'] = artifacts['run-log.json']
        if record.get('fragments'):
            observed = observation.get('log') or {}
            if (record.get('status') != 'partial' or record.get('complete') is not False
                    or record.get('observed') != observed
                    or set(record['fragments']) != {'run.log.head', 'run.log.tail'}):
                raise ValueError('Original run log fragment metadata changed')
            log.update(original_bytes=observed['bytes'], original_sha256=observed['sha256'], fragments={})
            for name, part in sorted(record['fragments'].items()):
                path = folder / name
                if (path.resolve() != path or not path.is_file() or type(part.get('offset')) is not int
                        or type(part.get('bytes')) is not int or not 0 < part['bytes'] <= LOG_PAGE_BYTES
                        or not 0 <= part['offset'] <= observed['bytes'] - part['bytes']
                        or path.stat().st_size != part['bytes'] or sha(path) != part.get('sha256')):
                    raise ValueError('Original retained log fragment identity or length changed')
                data = path.read_bytes(); retain(name, data)
                log['fragments'][name] = {**part, 'artifact': artifacts[name]}
                texts.append((name, data.decode('utf-8', errors='replace')))
        elif record.get('status') in {'completed', 'partial'}:
            path = folder / 'run.log'; observed = observation.get('log') or {}
            if (record.get('path') != 'run.log' or path.resolve() != path or not path.is_file()
                    or type(record.get('bytes')) is not int or not 0 <= record['bytes'] <= MAX_LOG_BYTES
                    or path.stat().st_size != record['bytes'] or sha(path) != record.get('sha256')
                    or observed.get('bytes') != record['bytes'] or observed.get('sha256') != record['sha256']
                    or record.get('complete') is not (observed.get('complete') is True)):
                raise ValueError('Original retained run log identity or length changed')
            data = path.read_bytes(); retain('run.log', data)
            log['artifact'] = artifacts['run.log']
            texts.append(('run.log', data.decode('utf-8', errors='replace')))
    if 'run.log' not in artifacts and observation.get('log_tail'):
        # Old receipts remain readable offline. A tail is explicitly incomplete.
        retain('legacy-log-tail.txt', observation['log_tail'].encode())
        log['legacy_tail'] = artifacts['legacy-log-tail.txt']
    for field in ('message', 'traceback'):
        text = result.get(field)
        if not isinstance(text, str) or not text: continue
        retain(field + '.txt', text.encode())
        texts.append((field + '.txt', text))

    def first_matching_line(pattern, flags=0):
        for field, text in texts:
            lines = text.splitlines()
            for number, line in enumerate(lines, 1):
                if re.search(pattern, line, flags):
                    context = []
                    if number > 1 and re.search(r':\d+(?::\d+)?:\s*$', lines[number - 2]):
                        previous = lines[number - 2].encode()
                        context.append({'text': previous[:1024].decode('utf-8', errors='ignore'),
                                        'line': number - 1, 'artifact': field,
                                        'truncated': len(previous) > 1024})
                    return {'text': line[:2048], 'line': number, 'artifact': field,
                            'truncated': len(line) > 2048, 'context_before': context,
                            'read': artifacts[field].get('read')}
        return None

    # A C++ symbol such as c10::Error::Error is a stack frame, not a diagnostic.
    # Search both retained texts before falling back to a runtime exception.
    first = first_matching_line(r'\b(?:fatal )?error\s*:(?!:)|static assertion failed', re.IGNORECASE)
    if first is None:
        first = first_matching_line(r'CMake Error')
    runtime_error = None if first else first_matching_line(
        r'^\s*(?:[\w.]+\.)?\w+(?:Error|Exception)\s*:(?!:)|\[(?:ERROR|FATAL)\]|\bERR\d{5}\b')
    diagnostics = result.get('diagnostics') or {}
    reports = []
    for kind in ('ir', 'profiler'):
        found = [row for row in diagnostics.get('reports', []) if row.get('kind') == kind]
        row = found[0] if len(found) == 1 else {}
        reports.append({'kind': kind, 'status': row.get('status', 'unavailable'),
            'available': row.get('status') == 'completed' and bool(row.get('artifacts')),
            'artifact_count': len(row.get('artifacts') or {}),
            'reason': str(row.get('reason') or ('Original job has no unique diagnostic report' if not row else ''))[:1024]})
    return {'status': result['status'], 'correct': result.get('correct'),
        'case_count': len(result.get('cases') or []), 'geomean_speedup': result.get('geomean_speedup'),
        'error_type': str(result.get('error') or '')[:256], 'first_compiler_diagnostic': first,
        'first_runtime_error': runtime_error, 'run_log': log,
        'build_completed': result.get('build_completed'), 'failure_stage': result.get('failure_stage'),
        'error_excerpt': None if first else (runtime_error['text'] if runtime_error else str(result.get('message') or '')[:2048]),
        'diagnostics': {'required': diagnostics.get('required'), 'satisfied': diagnostics.get('satisfied'), 'reports': reports},
        'artifacts': artifacts, 'native_candidate_sha256': evidence['native_candidate_sha256'],
        'remote_result_sha256': evidence['remote_result_sha256'],
        'qualification': 'Original terminal evaluation feedback only; success still requires original F/G/H receipts.'}


READ_ARTIFACT = r'''import base64,hashlib,json,os,stat,sys
from pathlib import Path
r=json.load(sys.stdin); root=Path(sys.argv[1]); relative=Path(sys.argv[2]); path=root/relative
if (not root.is_absolute() or root.resolve()!=root or relative.is_absolute() or '..' in relative.parts
    or path.resolve()!=path or not path.is_relative_to(root) or not path.is_file()):raise ValueError('Original artifact is missing, linked or outside bounded job')
folder=root.parent
def original_file(name):
 p=folder/name
 if p.resolve()!=p or not p.is_file():raise ValueError('Original job evidence missing or linked: '+name)
 return p
def wire(handle):
 value=dict(handle); stamp=value.get('started_ns')
 if stamp is not None:
  if isinstance(stamp,bool) or not isinstance(stamp,(str,int)) or str(int(stamp))!=str(stamp):raise ValueError('Invalid exact original timestamp')
  value['started_ns']=str(stamp)
 return value
handle=json.loads(original_file('launch.json').read_text())
if wire(handle)!=wire(r['handle']) or handle.get('attempt')!=str(folder):raise ValueError('Original diagnostic handle changed')
intent=json.loads(original_file('intent.json').read_text())
if intent.get('attempt',intent.get('round'))!=r['attempt'] or intent.get('source_sha256')!=r['source_sha256']:raise ValueError('Original diagnostic intent changed')
if hashlib.sha256(original_file('candidate.py').read_bytes()).hexdigest()!=r['source_sha256']:raise ValueError('Original diagnostic candidate changed')
if hashlib.sha256(original_file('job/result.json').read_bytes()).hexdigest()!=r['result_sha256']:raise ValueError('Original diagnostic result changed')
offset=r['offset']; limit=r['limit']
if type(offset)is not int or offset<0 or type(limit)is not int or not 1<=limit<=262144:raise ValueError('Invalid bounded diagnostic page')
with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
 before=os.fstat(stream.fileno()); identity=lambda s:[s.st_dev,s.st_ino,s.st_mtime_ns,s.st_size]
 if not stat.S_ISREG(before.st_mode) or before.st_size<=0:raise ValueError('Original artifact is missing or empty')
 if before.st_size>__MAX_ARTIFACT__:
  print(json.dumps({'status':'unavailable','reason':'Original artifact exceeds the __MAX_ARTIFACT__-byte collection bound','bytes':before.st_size,'expected_sha256':r['sha256']}));sys.exit()
 if offset>before.st_size:raise ValueError('Diagnostic offset exceeds original file')
 if r.get('identity') is not None and r['identity']!=identity(before):raise ValueError('Original diagnostic artifact changed during collection')
 if offset==0:
  digest=hashlib.sha256()
  while chunk:=stream.read(65536):digest.update(chunk)
  if digest.hexdigest()!=r['sha256']:raise ValueError('Original diagnostic artifact hash changed')
 stream.seek(offset); data=stream.read(limit)
 if identity(os.fstat(stream.fileno()))!=identity(before):raise ValueError('Original diagnostic artifact changed during read')
print(json.dumps({'status':'completed','bytes':before.st_size,'identity':identity(before),'offset':offset,
 'sha256':r['sha256'],'page_sha256':hashlib.sha256(data).hexdigest(),'data':base64.b64encode(data).decode()}))
'''.replace('__MAX_ARTIFACT__', str(MAX_ARTIFACT))


def download_artifact(run, evidence, relative, expected, destination, remaining):
    """Reconstruct one original diagnostic without a large transport frame."""
    if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
        raise ValueError('Original diagnostic artifact hash is invalid')
    request = {'handle': evidence['original_handle'], 'source_sha256': evidence['native_candidate_sha256'],
               'result_sha256': evidence['remote_result_sha256'], 'attempt': evidence['attempt'],
               'sha256': expected, 'offset': 0, 'limit': LOG_PAGE_BYTES}
    temporary = destination.with_name(destination.name + '.tmp')
    digest = hashlib.sha256(); total = None; offset = 0
    try:
        with temporary.open('wb') as stream:
            while True:
                reply = invoke_remote(run, READ_ARTIFACT, evidence['original_job'], relative, body=request)
                if reply.get('status') == 'unavailable':
                    return {'path': relative, 'expected_sha256': expected, 'status': 'unavailable',
                            'bytes': reply.get('bytes'), 'reason': str(reply.get('reason', 'Artifact not collected'))[:2048]}
                size = reply.get('bytes')
                if type(size) is not int or not 0 < size <= MAX_ARTIFACT or (total is not None and size != total):
                    raise ValueError('Original diagnostic artifact length changed')
                if size > remaining:
                    return {'path': relative, 'expected_sha256': expected, 'status': 'unavailable', 'bytes': size,
                            'reason': 'Diagnostic set exceeds the bounded local retention budget'}
                data = base64.b64decode(reply['data'], validate=True)
                if (reply.get('sha256') != expected or reply.get('offset') != offset
                        or len(data) != min(LOG_PAGE_BYTES, size - offset)
                        or hashlib.sha256(data).hexdigest() != reply.get('page_sha256')
                        or (request.get('identity') is not None and reply.get('identity') != request['identity'])):
                    raise ValueError('Original diagnostic artifact page identity or hash changed')
                total = size; digest.update(data); stream.write(data); offset += len(data)
                if offset == total: break
                request.update(offset=offset, identity=reply['identity'])
            stream.flush(); os.fsync(stream.fileno())
        if digest.hexdigest() != expected: raise ValueError('Original diagnostic artifact hash changed')
        temporary.replace(destination)
        return {'status': 'completed', 'bytes': total, 'sha256': expected}
    finally:
        temporary.unlink(missing_ok=True)


def diagnostic(config, request, kind, result, evidence):
    found = [row for row in (result.get('diagnostics') or {}).get('reports', []) if row.get('kind') == kind]
    if len(found) != 1:
        return {'kind': kind, 'status': 'unavailable', 'reason': 'Original job did not collect this diagnostic; no replacement job was launched', **evidence}
    report = found[0]
    paths = {**report.get('attempt_evidence', {}), **report.get('artifacts', {})}
    local = Path(request['output']) / 'artifacts'; local.mkdir()
    copied = {}; unavailable = []; remaining = 4 * MAX_ARTIFACT
    # Keep diagnostic failures and any validated partial output together.
    # The bounded index tells readers explicitly if more original files exist.
    for index, (relative, expected) in enumerate(sorted(paths.items())[:32]):
        name = f'{kind}-{index}' + Path(relative).suffix
        collected = download_artifact(transport(config), evidence, relative, expected, local / name, remaining)
        if collected['status'] != 'completed':
            unavailable.append(collected); continue
        remaining -= collected['bytes']; copied['artifacts/' + name] = expected
    incomplete = bool(unavailable) or len(paths) > 32
    status = ('failed' if copied else 'unavailable') if incomplete else report.get('status', 'completed')
    return {'kind': kind, 'status': status, 'reason': report.get('reason'),
            **evidence, 'scope': 'npu_diagnostic', 'candidate_sha256': request['source']['sha256'],
            'partial': report.get('partial', False) or (incomplete and bool(copied)), 'artifacts': copied, 'original_artifacts': paths,
            'attempt_evidence': report.get('attempt_evidence', {}), 'artifact_index_truncated': len(paths) > 32,
            'collection_complete': not incomplete, 'unavailable_artifacts': unavailable}


def pairing_record(request, result, evidence, environment):
    measurement = request['contract']['measurement']
    noise = result.get('noise_calibration')
    if not noise:
        if measurement.get('require_noise'): raise ValueError('Original job lacks independent eager/eager calibration')
        return None
    if noise.get('schema_version') != 'kernelbridge.noise-calibration/v1' or noise.get('protocol') != 'independent_eager_aaaa':
        raise ValueError('Unsupported native null-calibration protocol')
    for name in ('pairs', 'warmup', 'repeats', 'device', 'baseline_sha256', 'driver_sha256', 'source_sha256', 'environment'):
        if noise.get(name) != result.get(name): raise ValueError('Native calibration identity differs: ' + name)
    expected_inputs = {case['id']: case['input_sha256'] for case in result['cases']}
    if {case['id']: case['input_sha256'] for case in noise['cases']} != expected_inputs:
        raise ValueError('Native calibration cases or inputs differ')
    raw = (json.dumps(noise, indent=2, allow_nan=False) + '\n').encode()
    noise_sha = hashlib.sha256(raw).hexdigest()
    if noise_sha != result.get('noise_calibration_sha256'): raise ValueError('Original noise file hash differs')
    binding = {'source': request['binding']['source'], 'baseline': request['binding']['baseline'],
               'contract': request['binding']['contract'], 'oracle': request['contract']['oracle']['files']['sha256'],
               'evaluator': result['driver_sha256'], 'protocol': measurement['protocol_id'], 'environment': environment}
    (Path(request['output']) / 'noise.json').write_bytes(raw)
    return {'binding': binding, 'cases': result['cases'], 'calibration': {**noise, 'binding': binding,
            'receipt': {'path': evidence['original_job'] + '/noise.json', 'sha256': noise_sha,
                        'payload_sha256': manifest_sha(noise)}}}


def execute(config, operation, request):
    if 'wait_seconds' in request:
        remaining = request['wait_seconds']
        if type(remaining) not in (int, float) or not 0 <= remaining <= 1800:
            raise ValueError('Bounded native wait must be between 0 and 1800 seconds')
        config = dict(config, wait_seconds=min(config.get('wait_seconds', 900), remaining),
                      poll_seconds=min(config.get('poll_seconds', 5), max(0.05, remaining)))
    verify(request['source'])
    try:
        verify_contract(config, request)
    except ContractMismatch as error:
        if operation != 'launch':
            raise
        rejection, artifacts = make_rejection(request, error.expected, error.actual)
        for name, data in artifacts.items():
            write_preflight_artifact(Path(request['output']) / name, data)
        return rejection
    if operation == 'launch':
        if set(request['source']['files']) != {'candidate.py'}: raise ValueError('Freeze exactly candidate.py')
        action_id = request.get('action_id')
        if not isinstance(action_id, str) or not action_id: raise ValueError('Original pipeline tool action ID is required')
        source_sha = request['source']['files']['candidate.py']
        stage = Path(config['state']).parent / 'trial-inputs' / hashlib.sha256(action_id.encode()).hexdigest()
        stage.mkdir(parents=True, exist_ok=True)
        candidate = stage / 'candidate.py'
        if candidate.exists() and sha(candidate) != source_sha: raise ValueError('Staged frozen source changed')
        if not candidate.exists(): candidate.write_bytes((Path(request['source']['root']) / 'candidate.py').read_bytes())
        events = sorted(Path(config['pipeline_state']).glob('event-*.json'))
        hypothesis = json.loads(events[-1].read_text())['body'].get('C') if events else None
        if not hypothesis: raise ValueError('Record pipeline_plan C before launching')
        boundary = {'protocol': PROTOCOL, 'operation': 'launch', 'action_id': action_id,
                    'request_sha256': preflight_digest(request), 'source_sha256': request['source']['sha256']}
        write_preflight_artifact(Path(request['output']) / 'dispatch-boundary.json',
                                 (json.dumps(boundary, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n').encode())
        outcome = trial(dict(config, candidate=str(candidate)), {'id': action_id, 'arguments': {'hypothesis': json.dumps(hypothesis, ensure_ascii=False)}})
        if outcome['status'] != 'completed': raise RuntimeError('Original NPU job unresolved; recover this launch with npu-collect')
        save(Path(config['candidate']).parent / 'feedback.json', outcome['data'])
    elif operation == 'collect':
        folder, intent = round_for(config, request)
        if not (folder / 'result.json').exists():
            outcome = trial(config, recover=attempt_number(intent))
            if outcome['status'] != 'completed': raise RuntimeError('Original NPU job is still unresolved')
    folder, result, summary, evidence = original(config, request)
    if operation == 'collect' and request.get('handle') and (folder / 'run-log.json').is_file():
        log = json.loads((folder / 'run-log.json').read_text())
        verify_log_binding(log, evidence)
        if log.get('status') != 'completed':
            recover_run_log(folder, transport(config))
    if operation in ('launch', 'collect'):
        return {'status': 'completed', 'phase': 'complete', 'handle': summary['original_handle'], 'job_status': result['status'],
                'evaluation': terminal_feedback(folder, request, result, evidence),
                'execution': 'E executed the complete native job once; F/G/J only project this original receipt', **evidence,
                'read_only_projection': operation == 'collect'}
    cases = result.get('cases', [])
    expected = sorted(case['id'] for case in request['contract']['suite'])
    correct = result.get('correct') is True and summary.get('correct') is True and sorted(case['id'] for case in cases) == expected
    if operation == 'check':
        return {'checks': {'build': result.get('build_completed', result['status'] == 'completed') is True, 'semantics': correct, 'full': correct},
                'case_count': len(cases), 'case_ids': [case['id'] for case in cases],
                'oracle_sha256': request['contract']['oracle']['files']['sha256'], 'error': result.get('message'), **evidence}
    if operation == 'measure':
        arm = int(Path(request['output']).name.removeprefix('arm-'))
        if arm not in range(4) or request['role'] != 'ABBA'[arm]: raise ValueError('Expected one original ABBA arm index')
        source_files = request['source']['files']
        if request['role'] == 'A':
            if source_files != {config.get('baseline_file', 'npu_eager.py'): result['baseline_sha256']}: raise ValueError('Baseline differs from original eager source')
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
        environment = dict(result['environment'], device_index=result['device'], soc=result['soc'],
                           original_result_sha256=evidence['remote_result_sha256'])
        pairing = pairing_record(request, result, evidence, environment)
        return {'correct': correct, 'cases': samples, 'measurement': request['contract']['measurement'],
                'environment': environment, 'pairing': pairing,
                'original_arm_index': arm, 'new_measurements': 0,
                'original_aggregation': {'candidate': {case['id']: case['candidate'] for case in cases},
                    'eager': {case['id']: case['eager'] for case in cases},
                    'speedups': {case['id']: case['speedup'] for case in cases},
                    'geomean_speedup': result['geomean_speedup']}, **evidence}
    if operation in ('ir', 'profiler'):
        return diagnostic(config, request, operation, result, evidence)
    raise ValueError('Unknown adapter operation')


if __name__ == '__main__':
    config = json.loads(Path(sys.argv[1]).read_text())
    print(json.dumps(execute(config, sys.argv[2], json.load(sys.stdin)), ensure_ascii=False, allow_nan=False))
