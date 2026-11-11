"""Explicit, portable operator workflow tools for the governed harness."""
import hashlib
import json
from pathlib import Path
import shutil


def prepare(profile, output, python):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    config = dict(profile, state=str(output.parent / 'operator-pipeline'), python=str(Path(python).resolve()))
    config['read_roots'] = [str(Path(p).resolve(strict=True)) for p in profile['read_roots']]
    config['drivers'] = json.loads(json.dumps(profile.get('drivers', {})))
    for name, driver in config['drivers'].items():
        if not driver.get('files') or not driver.get('argv'):
            raise ValueError('Each driver needs explicit argv and pinned files: ' + name)
        driver['files'] = {str(Path(p).resolve(strict=True)): hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in driver['files']}
        if driver.get('kind') not in {'check', 'measure', 'launch', 'collect', 'diagnostic', 'calibrate'}:
            raise ValueError('Unknown driver kind')
    shutil.copyfile(Path(__file__).with_name('pipeline.py'), output / 'pipeline.py')
    (output / 'config.json').write_text(json.dumps(config, indent=2) + '\n')
    pins = [{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(output.iterdir())]
    string = {'type': 'string', 'minLength': 1}
    obj = {'type': 'object'}
    specs = [
        ('pipeline_bind', 'A: bind task, independent correctness oracle, measurement and optional performance baseline. Changing them invalidates earlier acceptance.',
         {'contract': obj}, ['contract']),
        ('pipeline_plan', 'B/C: record workload families or a falsifiable hypothesis, including retained diagnostic evidence.',
         {'stage': {'enum': ['B', 'C']}, 'record': obj}, ['stage', 'record']),
        ('pipeline_freeze', 'D: freeze explicit candidate files and origin before execution. Never evaluates mutable workspace bytes.',
         {'files': {'type': 'object', 'additionalProperties': string}, 'origin': obj}, ['files', 'origin']),
        ('pipeline_run', 'E/F/I/J: invoke a configured driver on frozen source. Collect with the original handle; after an uncertain launch use a collect driver with recover_run from pipeline_status. Never relaunch to recover.',
         {'stage': {'enum': ['E', 'F', 'I', 'J']}, 'driver': string, 'handle': obj, 'recover_run': string}, ['stage', 'driver']),
        ('pipeline_pair', 'G: run baseline-candidate-candidate-baseline measurements with the same pinned driver. No baseline means no improvement claim.',
         {'driver': string}, ['driver']),
        ('pipeline_promote', 'H: promote only source-bound complete correctness and, when a baseline exists, a positive full paired result. Bootstrap without baseline establishes an incumbent, not a speedup.', {}, []),
        ('pipeline_baseline', 'Select a frozen performance comparator or the last accepted incumbent. Keep semantic oracle separate. Old comparisons become stale.',
         {'selection': {'enum': ['incumbent', 'none', 'files']}, 'files': {'type': 'object', 'additionalProperties': string}}, ['selection']),
        ('pipeline_archive', 'I: archive accepted source bytes and retained evidence hashes, and record mechanisms/failure conditions. Does not submit or publish.',
         {'knowledge': obj}, ['knowledge']),
        ('pipeline_status', 'Read A-J workflow state, baseline identities, original handles, receipts and available drivers.', {}, []),
        ('pipeline_read', 'Read a bounded page from a hash-verified original workflow artifact.',
         {'run': string, 'path': string, 'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 16384}}, ['run', 'path']),
    ]
    return [{'name': name, 'description': description, 'parameters': {'type': 'object', 'properties': properties,
             'required': required, 'additionalProperties': False}, 'argv': [config['python'], '-B', str(output / 'pipeline.py'), str(output / 'config.json')],
             'files': pins, 'credits': 1, 'timeout': 3600, 'env_allowlist': []} for name, description, properties, required in specs]
