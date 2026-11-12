"""Verified original-job observations; these never settle an execution."""
import hashlib
import base64
import importlib.util
import json
import re
from pathlib import Path

from job_identity import same_handle, wire_handle

PROTOCOL = 'kernelbridge.driver-observation/v1'
FILES = {'intent.json', 'launch.json', 'observation.json', 'collection-uncertain.json',
         'observed-launch.json', 'observed-execution-request.json'}
MAX_BYTES = 4 * 1024 * 1024


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def read_documents(folder, expected=None):
    folder = Path(folder)
    if folder.resolve() != folder or not folder.is_dir():
        raise ValueError('Original observation folder is missing or linked')
    names = set(expected) if expected is not None else {name for name in FILES if (folder / name).exists()}
    if 'intent.json' not in names or names - FILES:
        raise ValueError('Original observation requires its bounded intent artifacts')
    documents = {}; hashes = {}; raw_files = {}
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: raise ValueError('Duplicate original observation field')
            value[key] = item
        return value
    for name in sorted(names):
        path = folder / name
        if path.resolve() != path or not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
            raise ValueError('Original observation artifact is missing, linked or too large')
        raw = path.read_bytes(); hashed = hashlib.sha256(raw).hexdigest()
        if len(raw) > MAX_BYTES or expected is not None and hashed != expected[name]:
            raise ValueError('Original observation artifact hash changed')
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict): raise ValueError('Original observation must be a JSON object')
        encoded(value)
        documents[name] = value; hashes[name] = hashed; raw_files[name] = raw
    return documents, hashes, raw_files


def launch_validator(path, expected_sha):
    """Load only an explicitly configured, pinned pure receipt validator."""
    path = Path(path)
    if path.resolve() != path or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
        raise ValueError('Original launch validator pin changed')
    spec = importlib.util.spec_from_file_location('kernelbridge_original_launch_validator', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.validate_launch_receipt


def envelope(request, operation, launch_action_id, documents, hashes, *, raw_files=None,
             validator=None, validator_sha256=None):
    if operation not in {'launch', 'collect'} or not isinstance(launch_action_id, str) or not launch_action_id:
        raise ValueError('Original launch action identity is required')
    intent = documents['intent.json']; source = request['source']
    if (intent.get('action_id') != launch_action_id or source['sha256'] != digest(source['files'])
            or request['binding']['source'] != source['sha256']
            or source['files'] != {'candidate.py': intent.get('source_sha256')}):
        raise ValueError('Observed original action/source differs from the requested launch')
    attempt = intent.get('remote_attempt')
    if not isinstance(attempt, str) or not attempt.startswith('/') or '..' in Path(attempt).parts:
        raise ValueError('Original remote attempt identity is invalid')
    acknowledgement = documents.get('launch.json', {})
    handle = acknowledgement.get('handle')
    observed = documents.get('observation.json')
    recovered = {'observed-launch.json', 'observed-execution-request.json'} & set(documents)
    if recovered:
        if len(recovered) != 2 or validator is None or raw_files is None or observed is None:
            raise ValueError('Recovered launch requires both original byte artifacts and a pinned validator')
        launch_raw = raw_files['observed-launch.json']; request_raw = raw_files['observed-execution-request.json']
        expected_intent = {**intent, 'supervisor_sha256': validator_sha256}
        if 'device' in request['contract']['measurement']:
            expected_intent['device'] = request['contract']['measurement']['device']
        original = validator(launch_raw, request_raw, intent=expected_intent)
        if (base64.b64decode(observed.get('original_launch_bytes', ''), validate=True) != launch_raw
                or base64.b64decode(observed.get('execution_request_bytes', ''), validate=True) != request_raw
                or observed.get('original_launch_sha256') != hashes['observed-launch.json']
                or observed.get('execution_request_sha256') != hashes['observed-execution-request.json']
                or not same_handle(original, observed.get('handle'))
                or handle is not None and not same_handle(original, handle)):
            raise ValueError('Recovered launch bytes differ from their original observation or acknowledgement')
        handle = original
    if observed is not None and observed.get('execution_exit') is not None:
        raise ValueError('Supervisor exit evidence requires its separate terminal validation')
    if handle is not None:
        if (not isinstance(handle, dict) or type(handle.get('pid')) is not int or handle['pid'] <= 0
                or handle.get('attempt') != attempt or not isinstance(handle.get('argv'), list)
                or not handle['argv'] or any(not isinstance(arg, str) for arg in handle['argv'])
                or 'started_ns' not in handle):
            raise ValueError('Original launch acknowledgement has an invalid handle')
        handle = wire_handle(handle)
        if observed is not None and not same_handle(observed.get('handle'), handle):
            raise ValueError('Observed handle differs from the original launch acknowledgement')
        if 'supervisor' in handle and observed is not None and observed.get('identity_verified') is not True:
            raise ValueError('Original supervised observation identity is unverified')
    elif observed is not None and observed.get('handle') is not None:
        # A PID supplied by a later observation cannot replace a missing ACK.
        handle = None
    if observed is not None and type(observed.get('alive')) is not bool:
        raise ValueError('Original observation lacks an explicit process state')
    state = {'status': 'unobserved' if observed is None or handle is None else 'running' if observed['alive'] else 'missing_result'}
    if observed is not None:
        provisional = observed.get('result')
        reported_sha = observed.get('result_sha256')
        if provisional is not None or reported_sha is not None:
            # A supervised child can write benchmark output before it exits.
            # Keep that observation visible without treating it as F/G evidence.
            if (not handle or 'supervisor' not in handle or observed.get('identity_verified') is not True
                    or not isinstance(provisional, dict) or provisional.get('source_sha256') != intent['source_sha256']
                    or not isinstance(reported_sha, str) or not re.fullmatch('[0-9a-f]{64}', reported_sha)):
                raise ValueError('Provisional benchmark observation is not bound to its original supervised job')
            state['provisional_result'] = {'authority': 'observation_only', 'payload_sha256': digest(provisional),
                'reported_bytes_sha256': reported_sha, 'bytes_hash_verified_locally': False}
        if handle and 'supervisor' in handle:
            # Only the original launch/request bytes prove the supervisor's
            # identity. A later liveness boolean alone never permits polling.
            verified_supervisor = bool(recovered) and observed.get('identity_verified') is True
            if verified_supervisor and type(observed.get('supervisor_alive')) is bool:
                state.update(supervisor_alive=observed['supervisor_alive'], supervisor_identity_verified=True)
            lifecycle = observed.get('lifecycle_state')
            if lifecycle == 'settling':
                state['status'] = ('settling' if verified_supervisor and observed.get('supervisor_alive') is True
                    and observed['alive'] is False else 'missing_exit')
            elif lifecycle == 'unknown':
                state['status'] = 'missing_exit'
        state['alive'] = observed['alive']
        log = observed.get('log')
        if isinstance(log, dict):
            state['log'] = {key: log[key] for key in ('bytes', 'sha256', 'stable', 'complete') if key in log}
    return {'protocol': PROTOCOL, 'status': 'unknown', 'phase': 'pending', 'operation': operation,
            'action_id': request['action_id'], 'request_sha256': digest(request),
            'source_sha256': source['sha256'], 'binding': request['binding'],
            'launch_action_id': launch_action_id, 'original_handle': handle,
            'observation_state': state, 'artifacts': hashes,
            'error': 'Original execution has no terminal receipt; recover its existing launch identity.'}
