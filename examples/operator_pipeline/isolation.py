"""Consume original sampled device-isolation evidence, never inspect a device."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat

ARTIFACTS = ('isolation-samples.jsonl', 'isolation-receipt.json',
             'execution-request.json', 'execution-exit.json')
MAX_BYTES = 16 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def policy_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def document(raw):
    def fields(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate isolation JSON field')
            value[key] = item
        return value
    def invalid(_value):
        raise ValueError('Nonfinite isolation JSON value')
    return json.loads(raw, object_pairs_hook=fields, parse_constant=invalid)


def read(path):
    path = Path(path)
    if path.resolve() != path:
        raise ValueError('Original isolation artifact is missing, escaped or too large')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
            raise ValueError('Original isolation artifact is missing, escaped or too large')
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Original isolation artifact grew beyond its bound')
    return raw


def requirement(measurement, configured=None):
    value = measurement.get('device_isolation')
    if configured is not None and digest(value) != digest(configured):
        raise ValueError('Declared isolation policy differs from the frozen profile')
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'policy', 'policy_sha256'}
            or not isinstance(value['policy'], dict) or value['policy'].get('required') is not True
            or value['policy_sha256'] != policy_digest(value['policy'])
            or value['policy'].get('logical_device') != measurement.get('device')):
        raise ValueError('Invalid required device-isolation policy binding')
    return value


def module(path, pins):
    path = Path(path)
    raw = read(path)
    if str(path) not in pins or hashlib.sha256(raw).hexdigest() != pins[str(path)]:
        raise ValueError('Isolation validator is not an unchanged pinned file')
    spec = importlib.util.spec_from_file_location('kernelbridge_pinned_isolation_' + pins[str(path)], path)
    value = importlib.util.module_from_spec(spec)
    exec(compile(raw, str(path), 'exec'), value.__dict__)
    return value


def verify(results, measurement, source_files, *, run_folder, driver, configured=None):
    required = requirement(measurement, configured)
    if required is None:
        return None
    if driver.get('kind') != 'measure' or not driver.get('isolation_validator'):
        raise ValueError('Required isolation needs a pinned measurement validator')
    pins = driver.get('files', {})
    for path, expected in pins.items():
        if hashlib.sha256(read(path)).hexdigest() != expected:
            raise ValueError('Pinned isolation driver source changed')
    identity_paths = [path for path in pins if Path(path).name == 'job_identity.py']
    if len(identity_paths) != 1:
        raise ValueError('Isolation requires one pinned original-handle comparator')
    identity = module(identity_paths[0], pins)
    validator = module(driver['isolation_validator'], pins)
    policy = required['policy']
    if validator.policy_digest(policy) != required['policy_sha256'] or digest(validator.normalize_policy(policy)) != digest(policy):
        raise ValueError('Isolation policy normalization changed its identity')
    if len(results) != 4:
        raise ValueError('Four isolation-bound ABBA projections required')
    descriptor = results[0].get('device_isolation')
    if (not isinstance(descriptor, dict)
            or descriptor.get('schema_version') != 'kernelbridge.device-isolation-evidence/v1'
            or descriptor.get('policy_sha256') != required['policy_sha256']
            or set(descriptor.get('artifacts', {})) != set(ARTIFACTS)
            or any(digest(row.get('device_isolation')) != digest(descriptor) for row in results)):
        raise ValueError('Original isolation projections are missing or inconsistent')
    root = Path(run_folder)
    if root.resolve() != root:
        raise ValueError('Original isolation run directory must be canonical')
    originals = None
    for position in range(4):
        raw = {name: read(root / ('arm-' + str(position)) / name) for name in ARTIFACTS}
        if any(hashlib.sha256(value).hexdigest() != descriptor['artifacts'][name] for name, value in raw.items()):
            raise ValueError('Original isolation artifact hash changed')
        if originals is not None and originals != raw:
            raise ValueError('ABBA arms refer to different original isolation bytes')
        originals = raw
    receipt = document(originals['isolation-receipt.json'])
    request = document(originals['execution-request.json'])
    exit_record = document(originals['execution-exit.json'])
    for field, name in (('isolation_code_sha256', Path(driver['isolation_validator']).name),
                        ('process_isolation_code_sha256', 'process_isolation.py'),
                        ('supervisor_sha256', 'npu_job_supervisor.py'),
                        ('npu_enter_code_sha256', 'npu_enter.py')):
        matches = [expected for path, expected in pins.items() if Path(path).name == name]
        if len(matches) != 1 or request.get(field) != matches[0]:
            raise ValueError('Original isolation execution code differs from the pinned driver closure')
    intent, handle = request['intent'], exit_record['handle']
    native_source = intent.get('source_sha256')
    if native_source not in source_files.values() or intent.get('device') != measurement.get('device'):
        raise ValueError('Isolation source or device differs from the measured candidate')
    for row in results:
        if (row.get('native_candidate_sha256') != native_source
                or not identity.same_handle(row.get('original_handle'), handle)
                or row.get('remote_result_sha256') != receipt.get('result_sha256')
                or not isinstance(row.get('remote_result_sha256'), str)
                or len(row['remote_result_sha256']) != 64):
            raise ValueError('Isolation evidence differs from original measured job/result')
    derived = validator.validate_evidence(originals['isolation-samples.jsonl'], originals['isolation-receipt.json'],
        originals['execution-request.json'], originals['execution-exit.json'], handle=handle, intent=intent, policy=policy)
    if derived.get('verdict') != 'sampled_observed_clear':
        raise ValueError('Required sampled device isolation was not observed clear')
    return {'policy_sha256': required['policy_sha256'], 'verdict': 'sampled_observed_clear',
            'scope': 'cooperative_same_uid_namespace', 'claim': 'sampled_observed_clear',
            'artifacts': descriptor['artifacts'], 'native_candidate_sha256': native_source,
            'remote_result_sha256': receipt['result_sha256'],
            'original_handle': identity.wire_handle(handle),
            'limitation': 'Sampled process observations and a cooperative same-UID lease; not absolute device exclusivity.'}
