"""Versioned proof of a pinned driver's rejection before remote dispatch.

This module never executes a driver or changes workflow state. Filesystem reads
only verify the original evidence returned by the declared driver protocol.
"""
import ast
import hashlib
import json
from pathlib import Path


PROTOCOL = 'kernelbridge.driver-preflight/v1'
ARTIFACTS = ('preflight/expected-contract.json', 'preflight/actual-contract.json',
             'preflight/differences.json')
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
SOURCE_ARTIFACT = 'preflight/source-validation.json'
NATIVE_SOURCE_FIELDS = ('host_operator_src', 'host_tiling_src', 'kernel_src',
                        'model_src', 'project_json_src', 'python_bind_src')


class SourceFormatError(ValueError):
    """A deterministic rejection of source bytes, before any execution."""
    def __init__(self, code, message, *, line=None, column=None):
        super().__init__(message)
        self.detail = {'code': code, 'message': message}
        if line is not None: self.detail['line'] = line
        if column is not None: self.detail['column'] = column


def parse_literal_sources(data, fields):
    """Parse source strings without evaluating candidate code.

    A leading Python module docstring is documentation. Every remaining statement
    must assign one of the declared fields exactly once to a literal string.
    """
    if isinstance(data, bytes):
        try:
            data = data.decode('utf-8')
        except UnicodeDecodeError as error:
            raise SourceFormatError('encoding', 'Candidate must be UTF-8 source') from error
    try:
        nodes = ast.parse(data, filename='<candidate>').body
    except (SyntaxError, ValueError) as error:
        raise SourceFormatError('syntax', getattr(error, 'msg', str(error)),
                                line=getattr(error, 'lineno', None),
                                column=getattr(error, 'offset', None)) from error
    if (nodes and isinstance(nodes[0], ast.Expr) and isinstance(nodes[0].value, ast.Constant)
            and isinstance(nodes[0].value.value, str)):
        nodes = nodes[1:]
    result = {}
    for node in nodes:
        detail = {'line': node.lineno, 'column': node.col_offset + 1}
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise SourceFormatError('statement', 'Candidate must contain only literal source assignments after its optional module docstring', **detail)
        name = node.targets[0].id
        if name not in fields or name in result:
            raise SourceFormatError('field', 'Unexpected or duplicate source assignment: ' + name, **detail)
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError) as error:
            raise SourceFormatError('literal', 'Source field must be a literal string: ' + name, **detail) from error
        if not isinstance(value, str):
            raise SourceFormatError('string', 'Source field must be a string: ' + name, **detail)
        result[name] = value
    missing = sorted(set(fields) - set(result))
    if missing:
        raise SourceFormatError('missing', 'Missing source fields: ' + ', '.join(missing))
    return result


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


def make_source_rejection(request, filename, fields):
    """Prove a pure format rejection against the original frozen source bytes."""
    if fields != list(NATIVE_SOURCE_FIELDS):
        raise ValueError('Source rejection must retain the native six-field declaration')
    source = request['source']; root = Path(source['root'])
    path = root / filename
    if (not isinstance(filename, str) or Path(filename).is_absolute() or '..' in Path(filename).parts
            or root.resolve() != root or path.resolve() != path or not path.is_file()
            or source['sha256'] != digest(source['files']) or filename not in source['files']):
        raise ValueError('Invalid original frozen source for format rejection')
    raw = path.read_bytes(); file_sha = hashlib.sha256(raw).hexdigest()
    if file_sha != source['files'][filename]:
        raise ValueError('Original frozen source changed during format validation')
    try:
        parse_literal_sources(raw, fields)
    except SourceFormatError as error:
        validation = {'schema_version': 'kernelbridge.literal-source-validation/v1',
                      'file': filename, 'sha256': file_sha,
                      'format': {'fields': fields, 'allow_module_docstring': True},
                      'error': error.detail}
    else:
        raise ValueError('A source format rejection requires invalid frozen source')
    artifacts = {SOURCE_ARTIFACT: encoded(validation) + b'\n'}
    message = 'Candidate source format rejected: ' + validation['error']['message']
    value = {'protocol': PROTOCOL, 'status': 'rejected', 'phase': 'preflight',
             'dispatch_outcome': 'not_started', 'operation': 'launch',
             'action_id': request['action_id'], 'request_sha256': digest(request),
             'source_sha256': source['sha256'], 'binding': request['binding'],
             'error': {'code': 'candidate_format', 'message': text_prefix(message, 2048),
                       'file': filename, 'detail': {key: validation['error'][key]
                           for key in ('code', 'line', 'column') if key in validation['error']},
                       'truncated': len(message.encode('utf-8')) > 2048},
             'evidence': {'source_validation_sha256': digest(validation),
                          'artifacts': {name: hashlib.sha256(data).hexdigest()
                                        for name, data in artifacts.items()}}}
    return value, artifacts


def original_document(folder, name, expected_sha):
    path = folder / name
    if (path.resolve() != path or not path.is_file()
            or not 0 < path.stat().st_size <= MAX_ARTIFACT_BYTES):
        raise ValueError('Preflight artifact is missing, escaped or too large')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError('Original preflight artifact hash changed')
    document = json.loads(raw)
    if raw != encoded(document) + b'\n':
        raise ValueError('Preflight evidence must retain canonical original JSON bytes')
    return document


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
    if isinstance(value.get('error'), dict) and value['error'].get('code') == 'candidate_format':
        if (not isinstance(evidence, dict) or set(evidence) != {'source_validation_sha256', 'artifacts'}
                or not isinstance(evidence['artifacts'], dict) or set(evidence['artifacts']) != {SOURCE_ARTIFACT}):
            raise ValueError('Invalid source rejection evidence fields')
        validation = original_document(folder, SOURCE_ARTIFACT, evidence['artifacts'][SOURCE_ARTIFACT])
        if digest(validation) != evidence['source_validation_sha256']:
            raise ValueError('Original source validation evidence changed')
        rebuilt, artifacts = make_source_rejection(request, validation['file'], validation['format']['fields'])
        if (encoded(value) != encoded(rebuilt)
                or encoded(validation) + b'\n' != artifacts[SOURCE_ARTIFACT]):
            raise ValueError('Source rejection differs from original frozen source validation')
        return value
    if not isinstance(evidence, dict) or set(evidence) != {
            'expected_contract_sha256', 'actual_contract_sha256', 'artifacts'}:
        raise ValueError('Invalid preflight rejection evidence fields')
    if not isinstance(evidence['artifacts'], dict) or set(evidence['artifacts']) != set(ARTIFACTS):
        raise ValueError('Preflight rejection requires all original contract/diff artifacts')
    documents = []
    for name in ARTIFACTS:
        documents.append(original_document(folder, name, evidence['artifacts'][name]))
    expected, actual, differences = documents
    if (digest(expected) != evidence['expected_contract_sha256']
            or digest(actual) != evidence['actual_contract_sha256']
            or encoded(differences) != encoded(contract_differences(expected, actual))):
        raise ValueError('Original preflight contract/difference evidence changed')
    rebuilt, _ = make_rejection(request, expected, actual)
    if encoded(value) != encoded(rebuilt):
        raise ValueError('Preflight rejection summary differs from its complete original evidence')
    return value
