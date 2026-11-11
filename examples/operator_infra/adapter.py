"""Finite adapters around reviewed operator infra; stdout is one Outcome."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(name, config):
    for path, expected in config.get('dependency_files', {}).items():
        if sha(path) != expected: raise ValueError('Pinned infra dependency changed: ' + Path(path).name)
    for directory in config['dependencies']:
        if directory not in sys.path:
            sys.path.append(directory)
    path = Path(__file__).parent / (name + '.py')
    spec = importlib.util.spec_from_file_location('kb_infra_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if name == 'lab':
        module.ROOT = Path(config['source_root'])
        module.REGISTRY = module.ROOT / 'knowledge.json'
        module.PRIMITIVES = module.ROOT / 'primitive_contracts.json'
        module.DESIGN = module.ROOT / 'operator_design.json'
        private_index = Path(config['state']) / 'runs.json'
        module.INDEX = private_index if private_index.exists() else module.ROOT / 'runs.json'
    return module


def path_in_roots(value, config):
    path = Path(value).resolve(strict=True)
    roots = [Path(p) for p in [*config['read_roots'], config['state']]]
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError('Path is outside configured operator evidence roots')
    return path


def checked_archive(path):
    with zipfile.ZipFile(path) as bundle:
        items = bundle.infolist()
        if sum(item.file_size for item in items) > 128 * 1024 * 1024 or len(items) > 4096:
            raise ValueError('Archive exceeds local extraction bounds')
        names = set()
        for item in items:
            name = Path(item.filename)
            if name.is_absolute() or '..' in name.parts or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Archive contains traversal or symlink')
            if str(name) in names: raise ValueError('Duplicate archive entry')
            names.add(str(name))


def run(action, config, output):
    args = action['arguments']; tool = action['tool']
    if tool == 'infra_ir_render':
        from ir_bridge import render_ir
        return render_ir([path_in_roots(p, config) for p in args['paths']], output / 'ir.html', 'llvm', args.get('function'))
    if tool == 'infra_trace_clean':
        from trace_lab import clean_capture
        return clean_capture(path_in_roots(args['path'], config), output / 'trace',
            **{key: path_in_roots(args[key], config) for key in ('identity', 'archive', 'binary', 'hardware') if key in args})
    if tool == 'infra_trace_pc':
        from trace_lab import inspect_pc
        return inspect_pc(path_in_roots(args['directory'], config), args['core'], args['pc'])
    if tool in {'infra_context', 'infra_design', 'infra_preflight', 'infra_index', 'infra_compare'}:
        lab = load('lab', config)
        if tool == 'infra_context': return lab.context(args['operator'], args.get('mechanism', ''))
        if tool == 'infra_design': return lab.design(args['operator'])
        if tool == 'infra_preflight': return lab.preflight(args['card'])
        if tool == 'infra_compare':
            records = {r['id']: r for r in lab.read(lab.INDEX)}
            return lab.compare(records[args['baseline']], records[args['candidate']])
        records = lab.collect(path_in_roots(args['root'], config), args['operator'],
                              lab.read(path_in_roots(args['environment'], config)), lab.read(path_in_roots(args['experiments'], config)))
        prior = {r['id']: r for r in lab.read(lab.INDEX)} if lab.INDEX.exists() else {}
        for record in records:
            old = prior.get(record['id'])
            if old and any(old.get(k) != record.get(k) for k in ['report_path', 'report_sha256']):
                raise ValueError('Existing experiment identity changed; retain it and use a new label')
            prior[record['id']] = record
        index = Path(config['state']) / 'runs.json'
        temporary = output / 'index.json'; lab.save(temporary, list(prior.values()))
        shutil.copyfile(temporary, index)
        return {'indexed': len(records), 'total': len(prior), 'index': str(index), 'sha256': sha(index)}
    if tool == 'infra_forge_audit':
        # Its upstream import path is relative to the original ops root.
        sys.path.append(str(Path(config['source_root']).parent / 'community/ForgeKernel/src'))
        return load('forge_bridge', config).inspect_campaign(path_in_roots(args['campaign'], config))
    if tool == 'infra_cannbench_query':
        path = args['path']
        if not path.startswith('/') or path.startswith('//') or any(c in path for c in '\r\n'):
            raise ValueError('Use a same-origin relative API path')
        api = load('cannbench_api', config)
        token = os.environ.get('CANNBENCH_TOKEN')
        if not token: raise ValueError('Host environment CANNBENCH_TOKEN is required')
        api.read_token = lambda key='GITCODE_TOKEN': token
        return api.request(path, method='GET')
    if tool == 'infra_release_prepare':
        source = path_in_roots(args['archive'], config)
        checked_archive(source)
        return load('release_source', config).prepare(source, output / 'release', output / 'release-manifest.json')
    if tool == 'infra_release_bind':
        checked_archive(path_in_roots(args['archive'], config))
        original = path_in_roots(args['manifest'], config)
        target = output / 'release-manifest.json'; shutil.copyfile(original, target)
        return load('release_source', config).bind(target, path_in_roots(args['archive'], config))
    if tool == 'infra_score_opportunities':
        report = path_in_roots(args['report'], config)
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).parent / 'dq_score_opportunities.py'),
            str(report), '--reduction', str(args.get('reduction', .1)), '--output', str(output / 'sensitivity.json')],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, text=True)
        (output / 'scoring.stdout').write_text(result.stdout); (output / 'scoring.stderr').write_text(result.stderr)
        if result.returncode: raise ValueError('Scoring replay failed; inspect retained logs')
        return json.loads((output / 'sensitivity.json').read_text())
    if tool == 'infra_trajectory':
        module = load('dq_trajectory', config)
        return {'scope': 'C++ syntax, not compiler semantics or performance', 'files': [
            {'path': str(path), 'sha256': sha(path), 'structure': module.cpp_structure(path.read_bytes())}
            for path in (path_in_roots(p, config) for p in args['paths'])]}
    if tool == 'infra_campaign_init':
        archive = path_in_roots(args['archive'], config)
        checked_archive(archive)
        destination = output / 'source'; destination.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            if sum(item.file_size for item in bundle.infolist()) > 128 * 1024 * 1024 or len(bundle.infolist()) > 4096:
                raise ValueError('Campaign archive exceeds local extraction bounds')
            for item in bundle.infolist():
                name = Path(item.filename)
                if name.is_absolute() or '..' in name.parts or (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Archive contains traversal or symlink')
                if item.is_dir(): continue
                path = destination / name; path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists(): raise ValueError('Duplicate archive entry')
                path.write_bytes(bundle.read(item))
        manifest = {'operator': args['operator'], 'parent_zip_sha256': sha(archive), 'parent': str(archive),
                    'source': str(destination), 'stage': 'baseline_pending', 'device_acceptance': False,
                    'legacy_initializer_sha256': config['legacy_initializer_sha256']}
        (output / 'campaign.json').write_text(json.dumps(manifest, indent=2) + '\n')
        return manifest
    if tool == 'infra_run_driver':
        driver = config['drivers'].get(args['driver'])
        if not driver: raise ValueError('Driver is not configured')
        for path, expected in driver['input_hashes'].items():
            if sha(path) != expected: raise ValueError('Pinned driver input changed')
        cwd = path_in_roots(driver['cwd'], config)
        return load('operator_lab', config).record(driver['argv'], cwd, output / 'driver',
            inputs=driver.get('inputs', []), timeout=driver.get('timeout', 300), kind=driver.get('kind', 'diagnostic'))
    raise ValueError('Unknown operator infra tool')


def main():
    config = json.loads(Path(sys.argv[1]).read_text()); action = json.load(sys.stdin)
    root = Path(config['state']); root.mkdir(parents=True, exist_ok=True)
    if action['tool'] == 'infra_read':
        args = action['arguments']; identity = args['run']
        if not re.fullmatch('[a-f0-9]{64}', identity): raise ValueError('Invalid original run identity')
        folder = root / identity; manifest = json.loads((folder / 'manifest.json').read_text())
        if 'path' not in args: return {'status': 'completed', 'data': manifest}
        name = args['path']; path = folder / name
        expected = manifest['files'].get(name)
        if not expected or path.resolve() != path or not path.is_relative_to(folder) or sha(path) != expected:
            raise ValueError('Artifact missing, linked or changed')
        offset = args.get('offset', 0); limit = min(args.get('limit', 8192), 16384)
        with path.open('rb') as stream: stream.seek(offset); data = stream.read(limit)
        return {'status': 'completed', 'data': {'text': data.decode(errors='replace'), 'next_offset': offset + len(data)}}
    identity = hashlib.sha256(action['id'].encode()).hexdigest(); output = root / identity
    output.mkdir(exist_ok=False)
    try:
        with (output / 'stdout.log').open('w') as log, redirect_stdout(log): result = run(action, config, output)
        status = 'completed'
    except Exception as error:
        status = 'failed'; result = {'error': str(error)}
    try:
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    except ValueError:
        status = 'failed'; text = json.dumps({'error': 'Upstream returned nonfinite JSON numbers; inspect original logs'})
    token = os.environ.get('CANNBENCH_TOKEN')
    if token and token in text: status = 'failed'; text = json.dumps({'error': 'Credential appeared in upstream output; withheld'})
    if token:
        for path in output.rglob('*'):
            if path.is_file() and not path.is_symlink():
                data = path.read_bytes()
                if token.encode() in data:
                    path.write_bytes(data.replace(token.encode(), b'[REDACTED]')); status = 'failed'
    source = Path(config['source_root'])
    (output / 'data-inputs.json').write_text(json.dumps({str(p): sha(p) for p in source.glob('*.json') if p.is_file()}, indent=2))
    (output / 'result.json').write_text(text)
    files = {str(p.relative_to(output)): sha(p) for p in sorted(output.rglob('*')) if p.is_file() and not p.is_symlink()}
    (output / 'manifest.json').write_text(json.dumps({'run': identity, 'tool': action['tool'], 'status': status, 'files': files}, indent=2))
    return {'status': status, 'data': {'run': identity, 'summary': json.loads(text) if len(text.encode()) < 12000 else 'Large report retained; use infra_read.',
                                    'files': list(files), 'device_acceptance': False}}


if __name__ == '__main__': print(json.dumps(main(), ensure_ascii=False))
