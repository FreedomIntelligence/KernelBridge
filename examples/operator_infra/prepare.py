"""Freeze explicitly selected local operator infra for the business-tool registry."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent


def prepare(profile, output, python):
    source = Path(profile['source_root']).resolve(strict=True)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    allowed = [str(Path(p).resolve(strict=True)) for p in profile.get('read_roots', [source.parent])]
    modules = ['lab.py', 'forge_bridge.py', 'cannbench_api.py', 'release_source.py',
               'dq_score_opportunities.py', 'dq_trajectory.py', 'operator_lab.py']
    pins = []
    for name in modules:
        path = source / name
        if not path.is_file() or path.is_symlink():
            raise ValueError('Required infra source missing or linked: ' + name)
        shutil.copyfile(path, output / name)
    shutil.copyfile(ROOT / 'adapter.py', output / 'adapter.py')
    diagnostics = ROOT.parent / 'cann/tools'
    for name in ('trace_lab.py', 'ir_bridge.py'):
        shutil.copyfile(diagnostics / name, output / name)
    shutil.copytree(diagnostics / 'vendor', output / 'vendor', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # The historical Engram initializer performs remote uploads and rewrites
    # other campaigns at import time. Record its identity; never execute it.
    legacy = source / 'start_engram_forge.py'
    config = {'source_root': str(source), 'read_roots': allowed, 'state': str(output.parent / 'operator-infra'),
              'dependencies': [str(source / name) for name in ['release_deps', 'trajectory_deps']],
              'legacy_initializer_sha256': hashlib.sha256(legacy.read_bytes()).hexdigest(),
              'drivers': profile.get('drivers', {}), 'python': str(Path(python).resolve())}
    for name, driver in config['drivers'].items():
        cwd = Path(driver['cwd']).resolve(strict=True)
        if not cwd.is_dir() or not any(cwd.is_relative_to(Path(root)) for root in allowed):
            raise ValueError('Driver cwd is outside read roots: ' + name)
        if not driver.get('inputs') or not driver.get('argv') or not all(isinstance(item, str) and item for item in driver['argv']):
            raise ValueError('Driver requires argv and explicit pinned inputs: ' + name)
        inputs = [(cwd / path).resolve(strict=True) for path in driver['inputs']]
        if any(not p.is_file() or not any(p.is_relative_to(Path(root)) for root in allowed) for p in inputs):
            raise ValueError('Driver input is outside read roots: ' + name)
        driver['cwd'] = str(cwd)
        driver['inputs'] = [str(p) for p in inputs]
        driver['input_hashes'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    config['dependency_files'] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in [*map(Path, config['dependencies']), source.parent / 'community/ForgeKernel/src']
        if directory.is_dir() for path in directory.rglob('*') if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}
    (output / 'config.json').write_text(json.dumps(config, indent=2) + '\n')
    for path in sorted(output.rglob('*')):
        if not path.is_file(): continue
        pins.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    string = {'type': 'string', 'minLength': 1}
    specs = [
        ('infra_context', 'Read existing operator context, prior experiments and mechanism evidence.', {'operator': string, 'mechanism': string}, ['operator']),
        ('infra_design', 'Read workload axes and task-bound design questions; does not establish correctness.', {'operator': string}, ['operator']),
        ('infra_preflight', 'Check an experiment card against evidence and prior failed assumptions.', {'card': {'type': 'object'}}, ['card']),
        ('infra_index', 'Index retained reports in this run; does not modify the shared ops index.', {'root': string, 'operator': string, 'environment': string, 'experiments': string}, ['root', 'operator', 'environment', 'experiments']),
        ('infra_compare', 'Compare two original indexed runs and their measured case results.', {'baseline': string, 'candidate': string}, ['baseline', 'candidate']),
        ('infra_forge_audit', 'Audit retained ForgeKernel milestone and paired evidence.', {'campaign': string}, ['campaign']),
        ('infra_cannbench_query', 'Read CANNBench metadata using GET only; no upload or submission.', {'path': string}, ['path']),
        ('infra_release_prepare', 'Prepare a local comment-clean release tree and source hash manifest; no publication.', {'archive': string}, ['archive']),
        ('infra_release_bind', 'Verify archive bytes against a prepared local release manifest.', {'manifest': string, 'archive': string}, ['manifest', 'archive']),
        ('infra_score_opportunities', 'Replay DQ scoring sensitivity; hypothetical gains are not measurements or admission gates.', {'report': string, 'reduction': {'type': 'number', 'exclusiveMinimum': 0, 'exclusiveMaximum': 1}}, ['report']),
        ('infra_trajectory', 'Read C++ syntax and source identity using the existing DQ trajectory parser.', {'paths': {'type': 'array', 'items': string, 'minItems': 1, 'maxItems': 64}}, ['paths']),
        ('infra_campaign_init', 'Create a fresh local campaign from an explicit frozen source archive, preserving the parent. No remote calls.', {'archive': string, 'operator': string}, ['archive', 'operator']),
        ('infra_run_driver', 'Run an explicitly configured synchronous build/check/profile/IR driver and retain original output identity.', {'driver': string}, ['driver']),
        ('infra_ir_render', 'Render explicit LLVM snapshots using pinned PyPTO; retain file order and hashes. Static text comparison, not PC mapping or timing.',
         {'paths': {'type': 'array', 'items': string, 'minItems': 2, 'maxItems': 8}, 'function': string}, ['paths']),
        ('infra_trace_clean', 'Clean a retained simulator capture without changing instruction identity/timing; bind source archive, binary and collector identity when supplied.',
         {'path': string, 'identity': string, 'archive': string, 'binary': string, 'hardware': string}, ['path']),
        ('infra_trace_pc', 'Inspect original core+PC metrics and raw events from a retained trace evidence directory.',
         {'directory': string, 'core': string, 'pc': string}, ['directory', 'core', 'pc']),
        ('infra_read', 'Read a bounded page of original infra output, or list its artifact manifest.', {'run': string, 'path': string, 'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 16384}}, ['run']),
    ]
    return [{'name': name, 'description': description,
             'argv': [config['python'], '-B', str(output / 'adapter.py'), str(output / 'config.json')],
             'files': pins, 'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False},
             'credits': 1, 'timeout': 1800 if name == 'infra_run_driver' else 120,
             'env_allowlist': ['CANNBENCH_TOKEN'] if name == 'infra_cannbench_query' else []}
            for name, description, properties, required in specs]


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--python', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(json.loads(args.profile.read_text()), args.output, args.python)))
