"""Versioned proof of a pinned driver's rejection before remote dispatch.

This module never executes a driver or changes workflow state. Filesystem reads
only verify the original evidence returned by the declared driver protocol.
"""
import hashlib
import json
from pathlib import Path


PROTOCOL = 'kernelbridge.driver-preflight/v1'
ARTIFACTS = ('preflight/expected-contract.json', 'preflight/actual-contract.json',
             'preflight/differences.json')
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def text_prefix(value, byte_limit):
    return value.encode('utf-8')[:byte_limit].decode('utf-8', errors='ignore')


def contract_differences(expected, actual):
    """Compare every JSON field, including numeric types and escaped pointers."""
    encoded(expected); encoded(actual)  # Reject nonfinite and non-JSON values.
    differences = []

    def visit(left, right, path):
        if type(left) is not type(right):
            differences.append({'path': path, 'kind': 'type', 'expected': left, 'actual': right})
        elif isinstance(left, dict):
            for key in sorted(set(left) | set(right)):
                if not isinstance(key, str):
                    raise ValueError('Contract object keys must be strings')
                child = path + '/' + key.replace('~', '~0').replace('/', '~1')
                if key not in left:
                    differences.append({'path': child, 'kind': 'extra', 'actual': right[key]})
                elif key not in right:
                    differences.append({'path': child, 'kind': 'missing', 'expected': left[key]})
                else:
                    visit(left[key], right[key], child)
        elif isinstance(left, list):
            for index in range(max(len(left), len(right))):
                child = path + '/' + str(index)
                if index >= len(left):
                    differences.append({'path': child, 'kind': 'extra', 'actual': right[index]})
                elif index >= len(right):
                    differences.append({'path': child, 'kind': 'missing', 'expected': left[index]})
                else:
                    visit(left[index], right[index], child)
        elif encoded(left) != encoded(right):
            differences.append({'path': path, 'kind': 'value', 'expected': left, 'actual': right})

    visit(expected, actual, '')
    return differences


def normalized_contract(request):
    contract = json.loads(encoded(request['contract']))
    contract['oracle']['files'] = contract['oracle']['files']['files']
    return contract


def make_rejection(request, expected, actual):
    """Return a bounded response and the complete immutable evidence bytes."""
    differences = contract_differences(expected, actual)
    if not differences:
        raise ValueError('A preflight rejection requires a real contract difference')
    if encoded(actual) != encoded(normalized_contract(request)):
        raise ValueError('Rejected contract differs from the original request')
    artifacts = dict(zip(ARTIFACTS, (encoded(value) + b'\n'
                                   for value in (expected, actual, differences))))
    preview = []
    for difference in differences[:8]:
        path = difference['path']
        preview.append({'path': text_prefix(path, 256), 'kind': difference['kind'],
                        **({'path_truncated': True} if len(path.encode('utf-8')) > 256 else {})})
    message = 'Declared contract differs: ' + ', '.join(
        (row['path'] or '<root>') + ' (' + row['kind'] + ')' for row in preview)
    value = {'protocol': PROTOCOL, 'status': 'rejected', 'phase': 'preflight',
             'dispatch_outcome': 'not_started', 'operation': 'launch',
             'action_id': request['action_id'], 'request_sha256': digest(request),
             'source_sha256': request['source']['sha256'], 'binding': request['binding'],
             'error': {'code': 'contract_mismatch', 'message': text_prefix(message, 2048),
                       'differences': preview, 'difference_count': len(differences),
                       'truncated': len(preview) != len(differences)
                                    or any(row.get('path_truncated') for row in preview)
                                    or len(message.encode('utf-8')) > 2048},
             'evidence': {'expected_contract_sha256': digest(expected),
                          'actual_contract_sha256': digest(actual),
                          'artifacts': {name: hashlib.sha256(data).hexdigest()
                                        for name, data in artifacts.items()}}}
    return value, artifacts


def validate_rejection(value, request, folder):
    """Validate exact request identity and complete original rejection evidence.

    The caller must separately enforce the driver's explicit protocol opt-in,
    source/driver pins, launch intent, receipt and committed manifest chain.
    """
    folder = Path(folder)
    marker = folder / 'dispatch-boundary.json'
    if folder.resolve() != folder or marker.exists() or marker.is_symlink():
        raise ValueError('Preflight proof has an invalid directory or dispatch boundary')
    if not isinstance(value, dict) or set(value) != {
            'protocol', 'status', 'phase', 'dispatch_outcome', 'operation', 'action_id',
            'request_sha256', 'source_sha256', 'binding', 'error', 'evidence'}:
        raise ValueError('Invalid preflight rejection fields')
    if (value['protocol'] != PROTOCOL or value['status'] != 'rejected'
            or value['phase'] != 'preflight' or value['dispatch_outcome'] != 'not_started'
            or value['operation'] != 'launch'
            or request.get('role') != 'E'
            or not isinstance(request.get('action_id'), str) or not request['action_id']
            or value['action_id'] != request['action_id']
            or value['request_sha256'] != digest(request)
            or value['source_sha256'] != request['source']['sha256']
            or encoded(value['binding']) != encoded(request['binding'])):
        raise ValueError('Preflight rejection request or source identity differs')
    evidence = value['evidence']
    if not isinstance(evidence, dict) or set(evidence) != {
            'expected_contract_sha256', 'actual_contract_sha256', 'artifacts'}:
        raise ValueError('Invalid preflight rejection evidence fields')
    if not isinstance(evidence['artifacts'], dict) or set(evidence['artifacts']) != set(ARTIFACTS):
        raise ValueError('Preflight rejection requires all original contract/diff artifacts')
    documents = []
    for name in ARTIFACTS:
        path = folder / name
        if (path.resolve() != path or not path.is_file()
                or not 0 < path.stat().st_size <= MAX_ARTIFACT_BYTES):
            raise ValueError('Preflight artifact is missing, escaped or too large')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != evidence['artifacts'][name]:
            raise ValueError('Original preflight artifact hash changed')
        document = json.loads(raw)
        if raw != encoded(document) + b'\n':
            raise ValueError('Preflight evidence must retain canonical original JSON bytes')
        documents.append(document)
    expected, actual, differences = documents
    if (digest(expected) != evidence['expected_contract_sha256']
            or digest(actual) != evidence['actual_contract_sha256']
            or encoded(differences) != encoded(contract_differences(expected, actual))):
        raise ValueError('Original preflight contract/difference evidence changed')
    rebuilt, _ = make_rejection(request, expected, actual)
    if encoded(value) != encoded(rebuilt):
        raise ValueError('Preflight rejection summary differs from its complete original evidence')
    return value
