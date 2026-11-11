"""Explicit, portable operator workflow tools for the governed harness."""
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import shutil


def prepare(profile, output, python):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    config = dict(profile, state=str(output.parent / 'operator-pipeline'), python=str(Path(python).absolute()))
    config['read_roots'] = [str(Path(p).resolve(strict=True)) for p in profile['read_roots']]
    config['drivers'] = json.loads(json.dumps(profile.get('drivers', {})))
    for name, driver in config['drivers'].items():
        if not driver.get('files') or not driver.get('argv'):
            raise ValueError('Each driver needs explicit argv and pinned files: ' + name)
        driver['files'] = {str(Path(p).resolve(strict=True)): hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in driver['files']}
        if driver.get('kind') not in {'check', 'measure', 'launch', 'collect', 'diagnostic', 'calibrate'}:
            raise ValueError('Unknown driver kind')
        timeout = driver.get('timeout', 600)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Driver timeout must be positive and finite')
    # The helper is copied beside the frozen pipeline, and also used by the NPU
    # benchmark inside its original process rather than dispatching a second job.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        spec = importlib.util.spec_from_file_location('kernelbridge_pipeline_contract', Path(__file__).with_name('pipeline.py'))
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    if profile.get('diagnostics') is not None:
        config['diagnostics'] = module.diagnostic_policy(config)
    # Leave enough time for the original evaluation plus its serial diagnostics;
    # the business-tool watchdog must not kill the diagnostic recovery boundary.
    diagnostic_seconds = sum(config['drivers'][name].get('timeout', 600)
        for name in (config.get('diagnostics') or {}).get('drivers', {}).values())
    driver_seconds = max((value.get('timeout', 600) * (4 if value['kind'] == 'measure' else 1)
        for value in config['drivers'].values()), default=0)
    tool_timeout = max(3600, driver_seconds + diagnostic_seconds + 30)
    shutil.copyfile(Path(__file__).with_name('pipeline.py'), output / 'pipeline.py')
    shutil.copyfile(Path(__file__).with_name('diagnostic_policy.py'), output / 'diagnostic_policy.py')
    shutil.copyfile(Path(__file__).with_name('qualification.py'), output / 'qualification.py')
    shutil.copyfile(Path(__file__).with_name('job_identity.py'), output / 'job_identity.py')
    (output / 'config.json').write_text(json.dumps(config, indent=2) + '\n')
    pins = [{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(output.iterdir())]
    string = {'type': 'string', 'minLength': 1}
    source_files = {'type': 'object', 'description': 'Map bundle-relative filenames to existing absolute source file paths. Values are paths, never source text or hashes.',
                    'additionalProperties': {'type': 'string', 'minLength': 1, 'description': 'Existing absolute source file path'}}
    obj = {'type': 'object'}
    specs = [
        ('pipeline_bind', 'A: bind task, independent correctness oracle, measurement and optional performance baseline. Changing them invalidates earlier acceptance.',
         {'contract': obj}, ['contract']),
        ('pipeline_plan', 'B/C: record workload families or a falsifiable hypothesis, including retained diagnostic evidence.',
         {'stage': {'enum': ['B', 'C']}, 'record': obj}, ['stage', 'record']),
        ('pipeline_freeze', 'D: freeze explicit candidate files and origin before execution. Never evaluates mutable workspace bytes.',
         {'files': source_files, 'origin': obj}, ['files', 'origin']),
        ('pipeline_run', 'E/F/I/J: invoke a configured driver on frozen source. F automatically runs configured IR/profiler diagnostics when policy requires them. Read returned diagnostics and revise the hypothesis in C. Collect with the original handle; after an uncertain launch use a collect driver with recover_run from pipeline_status. Never relaunch to recover.',
         {'stage': {'enum': ['E', 'F', 'I', 'J']}, 'driver': string, 'handle': obj, 'recover_run': string}, ['stage', 'driver']),
        ('pipeline_pair', 'G: run baseline-candidate-candidate-baseline measurements with the same pinned driver. Configured failure/regression/required policy invokes source-bound IR/profiler diagnostics. Read their actual artifacts before proposing the next candidate. No baseline means no improvement claim.',
         {'driver': string}, ['driver']),
        ('pipeline_promote', 'H: promote only source-bound complete correctness and, when a baseline exists, a positive full paired result. Bootstrap without baseline establishes an incumbent, not a speedup.', {}, []),
        ('pipeline_baseline', 'Select a frozen performance comparator or the last accepted incumbent. Keep semantic oracle separate. Old comparisons become stale.',
         {'selection': {'enum': ['incumbent', 'none', 'files']}, 'files': source_files}, ['selection']),
        ('pipeline_archive', 'I: archive accepted source bytes and retained evidence hashes, and record mechanisms/failure conditions. Does not submit or publish.',
         {'knowledge': obj}, ['knowledge']),
        ('pipeline_status', 'Start or resume an explicitly requested operator task here. Read durable A-J state, executable workflow.next_actions, required arguments, diagnostic evidence and original handles; then invoke the recommended tools autonomously within the user scope. Unavailable/unknown diagnostics are not completed analysis.', {}, []),
        ('pipeline_read', 'Read a bounded page from a hash-verified original workflow artifact.',
         {'run': string, 'path': string, 'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 16384}}, ['run', 'path']),
    ]
    return [{'name': name, 'description': description, 'parameters': {'type': 'object', 'properties': properties,
             'required': required, 'additionalProperties': False}, 'argv': [config['python'], '-B', str(output / 'pipeline.py'), str(output / 'config.json')],
             'files': pins, 'credits': 1, 'timeout': tool_timeout, 'env_allowlist': []} for name, description, properties, required in specs]
