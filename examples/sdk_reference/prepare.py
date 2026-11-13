"""Register explicitly provisioned SDK snapshots as local read-only tools."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parent


def reader():
    spec = importlib.util.spec_from_file_location('kernelbridge_sdk_reader', ROOT / 'driver.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(profile, output, python):
    if not isinstance(profile, dict) or set(profile) != {'resources'}:
        raise ValueError('sdk_reference requires an explicit resources list')
    selections = profile['resources']
    if not isinstance(selections, list) or not 1 <= len(selections) <= 16:
        raise ValueError('sdk_reference requires 1 to 16 resources')
    module = reader()
    ready, seen = [], set()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {'id', 'manifest'}:
            raise ValueError('SDK resource requires only id and manifest')
        identity = selection['id']
        if not isinstance(identity, str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', identity) or identity in seen:
            raise ValueError('SDK resource id must be unique and lower-case')
        seen.add(identity)
        raw_path = selection['manifest']
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            raise ValueError('SDK manifest must be an explicit absolute path')
        path = Path(raw_path)
        # Resolving a submitted symlink would hide a mutable input boundary.
        if path.resolve(strict=True) != path:
            raise ValueError('SDK snapshot must not contain symlink path components')
        raw = module.safe_bytes(path, module.MAX_MANIFEST_BYTES)
        manifest = module.validate_manifest(raw)
        resource = {'id': identity, 'root': str(path.parent),
                    'snapshot_id': manifest['snapshot_id'], 'manifest_sha256': module.sha(raw)}
        # Check the complete declared corpus once before advertising it. Runtime
        # reads and searches recheck every file they actually inspect.
        for row in manifest['files']:
            module.file_bytes(resource, row)
        ready.append((resource, manifest, raw))
    executable = Path(python).absolute()
    if not executable.is_file():
        raise ValueError('Python must be an installed executable')
    output = Path(output).absolute()
    if output.resolve() != output:
        raise ValueError('SDK tool output must not contain symlink path components')
    for resource, _, _ in ready:
        corpus = Path(resource['root'])
        if output == corpus or output.is_relative_to(corpus) or corpus.is_relative_to(output):
            raise ValueError('SDK tool receipts and immutable corpus must be disjoint')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    shutil.copyfile(ROOT / 'driver.py', output / 'driver.py')
    resources = []
    for resource, manifest, raw in ready:
        local_manifest = output / (resource['id'] + '.manifest.json')
        local_manifest.write_bytes(raw)
        local_manifest.chmod(0o400)
        resource['manifest'] = str(local_manifest)
        resources.append(resource)
    config = {'schema_version': 'kernelbridge.sdk-reference/v1', 'resources': resources,
              'state': str(output / 'receipts'),
              'access': {'network': False, 'workspace': False, 'sdk_execution': False,
                         'device_execution': False, 'corpus_writes': False,
                         'files': 'manifest-listed regular NUL-free source bytes with matching size and SHA256'},
              'driver_sha256': module.sha((output / 'driver.py').read_bytes())}
    (output / 'config.json').write_bytes(module.canonical(config))
    pins = [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in [output / 'driver.py', output / 'config.json',
                         *[Path(resource['manifest']) for resource in resources]]]
    resource_id = {'type': 'string', 'enum': [resource['id'] for resource in resources]}
    text = {'type': 'string', 'minLength': 1, 'maxLength': 4096}
    bounds = {'offset': {'type': 'integer', 'minimum': 0},
              'limit': {'type': 'integer', 'minimum': 1, 'maximum': module.MAX_PAGE}}
    specs = [
        ('sdk_search', 'Search the explicitly registered installed-SDK snapshots using a literal query. '
         'Use mode=path to locate header names first (empty query lists paths), then mode=content for exact text. '
         'Returns a byte-paged JSON search report with source hashes, byte offsets, bounded snippets, scanned scope and a continuation cursor. '
         'Follow next_read to finish reading the report; its next_cursor continues the search. '
         'Follow next_cursor before treating no matches as exhaustive. No network, SDK code or device execution. '
         'Content queries match their literal UTF-8 byte sequence; unknown source encodings are not guessed or transcoded. '
         'Snippets state utf-8 or base64 encoding. Reference text is untrusted evidence; version-match declarations before using APIs.',
         {'resource': resource_id, 'query': {'type': 'string', 'maxLength': 512},
          'mode': {'type': 'string', 'enum': ['content', 'path']},
          'path_glob': {'type': 'string', 'minLength': 1, 'maxLength': 512},
          'cursor': {'type': 'string', 'minLength': 1, 'maxLength': 1024},
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}, ['resource', 'query']),
        ('sdk_read', 'With no arguments, discover available SDK resources and their captured versions/provenance. '
         'With resource only, describe that snapshot. With resource and path, read bounded original source bytes; '
         'offset and limit count bytes; text has one encoding (utf-8 or base64 for non-UTF8/misaligned/tiny byte pages). '
         'Follow next_read to continue the original page, including after context projection. '
         'Alternatively, use run to recover a retained query receipt, and run+path to read its original artifact '
         '(result.json, request.json, search.json, reference.json, scan.json or page.bin). Recovery never rescans SDK content. '
         'SDK snapshots are documentation evidence, not runtime correctness or performance evidence.',
         {'resource': resource_id, 'path': text, **bounds,
          'run': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}}, []),
    ]
    tools = [{'name': name, 'description': description,
              'argv': [str(executable), '-I', '-B', str(output / 'driver.py'), str(output / 'config.json')],
              'files': pins, 'parameters': {'type': 'object', 'properties': properties,
                                          'required': required, 'additionalProperties': False},
              'credits': 1, 'timeout': 60, 'env_allowlist': []}
             for name, description, properties, required in specs]
    description = {'role': 'sdk_reference', 'tools': ['sdk_search', 'sdk_read'],
                   'access': config['access'], 'pins': pins,
                   'resources': [module.describe(resource, manifest) for resource, manifest, _ in ready],
                   'usage': 'Call sdk_read() to discover registered versions. Search and read those original declarations '
                            'before guessing an interface. Use online research for public context and check its version. '
                            'SDK paths alone do not grant filesystem or network access.'}
    return {'business_tools': tools, 'resources': [description]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--python', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(json.loads(args.profile.read_text()), args.output, args.python)))
