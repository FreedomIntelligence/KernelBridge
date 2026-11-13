"""Business-tool adapter for a frozen NPU attempt; reconnects to the original job.

argv: python -B npu_trial.py TRIAL_CONFIG.json; stdin: original Action JSON.
Recovery-only: add --recover ATTEMPT (never launches a replacement job).
"""
import base64
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

from npu_benchmark import sources
try:
    from job_identity import same_handle, wire_handle
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'operator_pipeline'))
    from job_identity import same_handle, wire_handle


def wire_summary(summary):
    """Keep durable evidence intact; avoid lossy OS identities on the JS wire."""
    projected = {**summary, 'original_handle': wire_handle(summary['original_handle'])} if 'original_handle' in summary else dict(summary)
    if isinstance(summary.get('run_log'), dict):
        log = dict(summary['run_log'])
        log['original_handle'] = wire_handle(log.get('original_handle'))
        if isinstance(log.get('observed'), dict):
            # Device/inode/nanosecond values remain exact in run-log.json. They
            # are not model inputs and need not cross JSON number consumers.
            log['observed'] = {key: value for key, value in log['observed'].items() if key != 'identity'}
        projected['run_log'] = log
    return projected


def implementation_digest(parsed):
    """Ignore metadata, comments and formatting when admitting a new hypothesis."""
    token = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*.*?\*/|[A-Za-z_]\w*|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\S', re.S)
    normalized = {key: [part for part in token.findall(parsed[key]) if not part.startswith(('//', '/*'))]
                  for key in ('host_tiling_src', 'host_operator_src', 'kernel_src', 'python_bind_src')}
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()


def save(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2, allow_nan=False); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


LAUNCH = r'''import hashlib,importlib.util,json,sys
from pathlib import Path
r=json.load(sys.stdin); root=Path(r['remote_root']); folder=Path(r['remote_attempt'])
if not root.is_absolute() or '..' in root.parts:raise ValueError('Invalid deployment root')
canonical=root.resolve(strict=True); module_path=canonical/'tools/npu_job_supervisor.py'
if module_path.resolve(strict=True)!=module_path:raise ValueError('Linked supervisor module')
if hashlib.sha256(module_path.read_bytes()).hexdigest()!=r['supervisor_sha256']:raise ValueError('Pinned supervisor source changed')
spec=importlib.util.spec_from_file_location('original_npu_job_supervisor',module_path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
argv=['python3',str(root/'tools/npu_enter.py'),str(root/'profile.json'),str(root/'tools'/r.get('benchmark_script','npu_benchmark.py')),'--candidate',str(folder/'candidate.py'),'--output',str(folder/'job'),'--mode',r['mode'],'--device',str(r['device']),'--warmup',str(r['warmup']),'--repeats',str(r['repeats']),'--pairs',str(r['pairs'])]
if r.get('profile'):argv.append('--profile')
print(json.dumps(module.launch(r,argv)))
'''

COLLECT = r'''import importlib.util,json,sys,hashlib,os,stat
from pathlib import Path
folder=Path(sys.argv[1]); path=folder/'job/result.json'; handle_path=folder/'launch.json'
handle=json.loads(handle_path.read_text()) if handle_path.exists() else None
request_raw=sys.stdin.read() if len(sys.argv)>2 and sys.argv[2]=='--expected' else ''; expected=json.loads(request_raw) if request_raw else None
lifecycle={}
if (isinstance(handle,dict) and 'supervisor' in handle) or (folder/'execution-request.json').exists():
 if not expected or not expected.get('remote_root') or not expected.get('supervisor_sha256'):raise ValueError('Supervised observation requires the original pinned deployment')
 root=Path(expected['remote_root']).resolve(strict=True); module_path=root/'tools/npu_job_supervisor.py'
 if module_path.resolve(strict=True)!=module_path:raise ValueError('Linked supervisor module')
 if hashlib.sha256(module_path.read_bytes()).hexdigest()!=expected['supervisor_sha256']:raise ValueError('Pinned supervisor source changed')
 spec=importlib.util.spec_from_file_location('original_npu_job_supervisor',module_path)
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 lifecycle=module.observe(folder,expected=expected);handle=lifecycle['handle'];alive=lifecycle['alive']
 declared=folder; folder=root/declared.relative_to(Path(expected['remote_root']))
 if folder.resolve(strict=True)!=folder:raise ValueError('Original supervised attempt contains a link')
 path=folder/'job/result.json'
 if (path.exists() or path.is_symlink()) and path.resolve()!=path:raise ValueError('Original benchmark result contains a link')
else:
 proc=Path('/proc')/str(handle['pid']) if handle else None
 alive=bool(proc and (proc/'cmdline').exists() and b'--candidate' in (proc/'cmdline').read_bytes())
result=json.loads(path.read_text()) if path.exists() else None
log=folder/'run.log'
if lifecycle and (log.exists() or log.is_symlink()) and log.resolve()!=log:raise ValueError('Original process log contains a link')
markers=(b'fall back to run on the CPU',b'npu_cpu_fallback')
overlap=b''; tail=b''; cpu_fallback=False; log_record=None
if log.exists():
 with os.fdopen(os.open(log,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
  before=os.fstat(stream.fileno())
  if not stat.S_ISREG(before.st_mode):raise ValueError('Original run.log is not a regular file')
  digest=hashlib.sha256(); size=0
  while chunk:=stream.read(65536):
   digest.update(chunk); size+=len(chunk)
   scanned=overlap+chunk
   cpu_fallback=cpu_fallback or any(marker in scanned for marker in markers)
   overlap=scanned[-(max(map(len,markers))-1):]
   tail=(tail+chunk)[-12000:]
  after=os.fstat(stream.fileno())
  identity=lambda s:[s.st_dev,s.st_ino,s.st_mtime_ns,s.st_size]
  stable=identity(before)==identity(after) and size==after.st_size
  log_record={'path':'run.log','bytes':size,'sha256':digest.hexdigest(),'identity':identity(after),'stable':stable,'complete':stable and not alive}
print(json.dumps({**lifecycle,'handle':handle,'alive':alive,'result':result,'result_sha256':hashlib.sha256(path.read_bytes()).hexdigest() if result else None,'cpu_fallback':cpu_fallback,'log_tail':tail.decode(errors='replace'),'log':log_record}))
'''


# Read only the observed original file, in bounded pages. No device work, log
# rewriting or replacement launch is permitted on this recovery path.
LOG_PAGE_BYTES = 256 * 1024
MAX_LOG_BYTES = 64 * 1024 * 1024
READ_RUN_LOG = r'''import base64,hashlib,importlib.util,json,os,stat,sys
from pathlib import Path
r=json.load(sys.stdin); declared=Path(r['remote_attempt']); root_arg=r.get('remote_root')
if not declared.is_absolute() or '..' in declared.parts:raise ValueError('Original attempt path is invalid')
if root_arg is None:
 folder=declared
 if folder.resolve()!=folder:raise ValueError('Original attempt alias requires a bound deployment root')
else:
 declared_root=Path(root_arg)
 if (not declared_root.is_absolute() or '..' in declared_root.parts
     or declared==declared_root or not declared.is_relative_to(declared_root)):raise ValueError('Original attempt is outside the bound deployment root')
 root=declared_root.resolve(strict=True); folder=root/declared.relative_to(declared_root)
 if folder.resolve(strict=True)!=folder:raise ValueError('Original attempt contains a link below its deployment root')
def original_file(name):
 path=folder/name
 if path.resolve()!=path or not path.is_file():raise ValueError('Original evidence missing or linked: '+name)
 return path
intent=json.loads(original_file('intent.json').read_text())
for key in ('action_id','attempt','source_sha256'):
 if intent.get(key)!=r[key]:raise ValueError('Original intent differs: '+key)
if hashlib.sha256(original_file('candidate.py').read_bytes()).hexdigest()!=r['source_sha256']:raise ValueError('Original candidate changed')
handle=json.loads(original_file('launch.json').read_text())
if (handle!=r['handle'] or handle.get('attempt')!=str(declared)
    or Path(handle['attempt']).resolve(strict=True)!=folder):raise ValueError('Original handle differs')
if root_arg is not None:
 argv=handle.get('argv',[])
 if (not isinstance(argv,list) or len(argv)<4
     or argv[1]!=str(declared_root/'tools/npu_enter.py')
     or argv[2]!=str(declared_root/'profile.json')
     or Path(argv[3]).parent!=declared_root/'tools'
     or not Path(argv[3]).name.startswith('npu_') or Path(argv[3]).suffix!='.py'):
  raise ValueError('Deployment root differs from the original launch handle')
if r.get('execution_exit_sha256') is not None:
 if root_arg is None:raise ValueError('Original supervisor log requires its bound deployment root')
 raw=original_file('execution-exit.json').read_bytes()
 if hashlib.sha256(raw).hexdigest()!=r['execution_exit_sha256']:raise ValueError('Original supervisor exit changed')
 module_path=root/'tools/npu_job_supervisor.py'
 if module_path.resolve(strict=True)!=module_path:raise ValueError('Linked supervisor module')
 if hashlib.sha256(module_path.read_bytes()).hexdigest()!=handle['supervisor']['code_sha256']:raise ValueError('Original supervisor code changed')
 spec=importlib.util.spec_from_file_location('original_npu_job_supervisor',module_path)
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 module.validate_exit_receipt(raw,handle=handle,intent=intent)
if r.get('result_sha256') is not None:
 if hashlib.sha256(original_file('job/result.json').read_bytes()).hexdigest()!=r['result_sha256']:raise ValueError('Original result changed')
elif r.get('execution_exit_sha256') is None:raise ValueError('Original log lacks a terminal evidence binding')
offset=r['offset']; limit=r['limit']; expected=r['log']
if type(offset)is not int or not 0<=offset<=expected['bytes'] or type(limit)is not int or not 1<=limit<=262144:raise ValueError('Invalid original log page')
path=original_file('run.log')
with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
 before=os.fstat(stream.fileno()); identity=lambda s:[s.st_dev,s.st_ino,s.st_mtime_ns,s.st_size]
 if not stat.S_ISREG(before.st_mode) or identity(before)!=expected['identity'] or before.st_size!=expected['bytes']:raise ValueError('Original log changed; collect the same handle again')
 stream.seek(offset); data=stream.read(min(limit,expected['bytes']-offset))
 if identity(os.fstat(stream.fileno()))!=expected['identity']:raise ValueError('Original log changed during read')
print(json.dumps({'offset':offset,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'data':base64.b64encode(data).decode()}))
'''


def capture_run_log(folder, intent, observation, run, *, remote_root=None):
    """Retain verified bytes without changing an original job's terminal status."""
    metadata = observation.get('log')
    binding = {'source_sha256': intent['source_sha256'], 'remote_attempt': intent['remote_attempt'],
               'original_handle': observation.get('handle'), 'remote_result_sha256': observation.get('result_sha256')}
    if observation.get('execution_exit_sha256') is not None:
        binding['execution_exit_sha256'] = observation['execution_exit_sha256']
    record = {'status': 'unavailable', 'complete': False, **binding}
    temporary = folder / 'run.log.tmp'
    try:
        if not isinstance(metadata, dict):
            raise ValueError('Original observation has no full-log metadata; legacy tail is not a complete log')
        record['observed'] = metadata
        size = metadata.get('bytes')
        if type(size) is not int or size < 0:
            raise ValueError('Original log length is invalid')
        if metadata.get('stable') is not True:
            raise ValueError('Original log was changing; recover the same attempt to read it again')
        if hashlib.sha256((folder / 'candidate.py').read_bytes()).hexdigest() != intent['source_sha256']:
            raise ValueError('Local original candidate identity changed')
        launch = folder / 'launch.json'
        if launch.exists():
            acknowledged = json.loads(launch.read_text()).get('handle')
            if acknowledged and not same_handle(acknowledged, observation.get('handle')):
                raise ValueError('Observed handle differs from the original launch acknowledgement')
        request = {**binding, 'handle': observation.get('handle'), 'result_sha256': observation.get('result_sha256'),
                   'action_id': intent['action_id'], 'attempt': attempt_number(intent), 'log': metadata}
        if remote_root is not None:
            request['remote_root'] = remote_root
            record['remote_root'] = remote_root
        def read_page(offset, limit=LOG_PAGE_BYTES):
            page = invoke_remote(run, READ_RUN_LOG, body={**request, 'offset': offset, 'limit': limit})
            data = base64.b64decode(page['data'], validate=True)
            if (page.get('offset') != offset or page.get('bytes') != len(data)
                    or len(data) != min(limit, size - offset)
                    or hashlib.sha256(data).hexdigest() != page.get('sha256')):
                raise ValueError('Original log page identity or length differs')
            return data
        if size > MAX_LOG_BYTES:
            # Preserve the beginning as well as the end, never disguise a tail
            # as a full file. The receipt keeps the original total size/hash.
            fragments = {}
            for name, offset in (('run.log.head', 0), ('run.log.tail', size - LOG_PAGE_BYTES)):
                data = read_page(offset)
                (folder / name).write_bytes(data)
                fragments[name] = {'offset': offset, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            record.update(status='partial', complete=False, fragments=fragments,
                          reason=f'Original log exceeds the {MAX_LOG_BYTES}-byte local retention bound; only explicit head/tail byte ranges retained.')
            save(folder / 'run-log.json', record)
            return record
        digest = hashlib.sha256()
        with temporary.open('wb') as stream:
            for offset in range(0, max(size, 1), LOG_PAGE_BYTES):
                data = read_page(offset)
                digest.update(data); stream.write(data)
            stream.flush(); os.fsync(stream.fileno())
        if digest.hexdigest() != metadata.get('sha256'):
            raise ValueError('Original complete log hash differs')
        temporary.replace(folder / 'run.log')
        record.update(status='completed' if metadata.get('complete') is True else 'partial', path='run.log',
                      bytes=size, sha256=digest.hexdigest(), complete=metadata.get('complete') is True)
        if not record['complete']:
            record['reason'] = 'All observed bytes retained; original process was still alive. Recover the same attempt for its final log.'
    except Exception as error:
        record['reason'] = str(error)[:2048]
        record['recovery'] = 'Read-only recovery of this original attempt; never launch a replacement for missing logs.'
    finally:
        temporary.unlink(missing_ok=True)
    save(folder / 'run-log.json', record)
    return record


def transport(config):
    spec = importlib.util.spec_from_file_location('task_npu_transport', config['transport_module'])
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.run


def recover_run_log(folder, run, *, remote_root=None):
    """Explicit log-only recovery; never replace terminal F/G measurements."""
    with (folder.parent / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        intent = json.loads((folder / 'intent.json').read_text())
        original = json.loads((folder / 'observation.json').read_text())
        record = json.loads((folder / 'run-log.json').read_text())
        try:
            metadata = (original.get('execution_exit') or {}).get('intent') or original.get('result') or {}
            observed = invoke_remote(run, COLLECT, intent['remote_attempt'], '--expected',
                                     body=observation_request({'remote_root': remote_root,
                                         **{key: metadata[key] for key in ('mode', 'device') if key in metadata}}, folder, intent))
        except Exception as error:
            record['recovery_error'] = str(error)[:2048]
            save(folder / 'run-log.json', record)
            return record
        if (observed.get('result_sha256') != original['result_sha256']
                or observed.get('result') != original['result']
                or not same_handle(observed.get('handle'), original.get('handle'))
                or observed.get('execution_exit_sha256') != original.get('execution_exit_sha256')):
            raise ValueError('Original result or handle changed during log-only recovery')
        if original.get('execution_exit_sha256') is not None:
            execution_exit(observed, intent)
        elif observed['result'].get('source_sha256') != intent['source_sha256']:
            raise ValueError('Original result source changed during log-only recovery')
        record = capture_run_log(folder, intent, observed, run, remote_root=remote_root)
        # Preserve original result, qualification, log-tail scan and measurement
        # receipt bytes. Only the separately bound log artifact is enriched.
        save(folder / 'observation.json', {**original, 'log': observed.get('log')})
        return record


def invoke_remote(run, script, *args, body=None):
    reply = run(['python3', '-B', '-c', script, *args], stdin=b'' if body is None else json.dumps(body).encode(), timeout=45)
    if reply['returncode']:
        raise RuntimeError(reply['stderr'].decode(errors='replace')[-4000:])
    return json.loads(reply['stdout'])


def observation_request(config, folder, intent):
    """Carry the original identity into a read-only observer, including lost ACKs."""
    request = {key: intent[key] for key in ('action_id', 'source_sha256', 'remote_attempt')}
    request.update(attempt=attempt_number(intent),
                   supervisor_sha256=hashlib.sha256(Path(__file__).with_name('npu_job_supervisor.py').read_bytes()).hexdigest(),
                   **isolation_request_fields(config))
    for key in ('mode', 'device'):
        if key in config: request[key] = config[key]
        elif key in intent: request[key] = intent[key]
    if config.get('remote_root') is not None:
        request['remote_root'] = config['remote_root']
    handle, _ = original_acknowledgement(folder, intent)
    if handle is not None: request['handle'] = handle
    return request


def retain_original_bytes(path, raw):
    """Copy a received original file once; never rewrite it from a projection."""
    if path.resolve() != path or path.is_symlink():
        raise ValueError('Retained original evidence contains a link: ' + path.name)
    if path.exists():
        if path.read_bytes() != raw: raise ValueError('Retained original evidence changed: ' + path.name)
        return
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(directory)
    finally: os.close(directory)


def original_acknowledgement(folder, intent):
    launch = folder / 'launch.json'
    if launch.is_file():
        handle = json.loads(launch.read_text()).get('handle')
        if handle is not None: return handle, launch
    observed = folder / 'observed-launch.json'
    request = folder / 'observed-execution-request.json'
    if observed.is_file() and request.is_file():
        from npu_job_supervisor import validate_launch_receipt
        handle = validate_launch_receipt(observed.read_bytes(), request.read_bytes(), intent=intent)
        return handle, observed
    return None, launch if launch.is_file() else None


def retain_observed_launch(config, folder, intent, observation):
    from npu_job_supervisor import validate_launch_receipt
    launch_raw = base64.b64decode(observation['original_launch_bytes'], validate=True)
    request_raw = base64.b64decode(observation['execution_request_bytes'], validate=True)
    if (hashlib.sha256(launch_raw).hexdigest() != observation['original_launch_sha256']
            or hashlib.sha256(request_raw).hexdigest() != observation['execution_request_sha256']):
        raise ValueError('Original observed launch bytes changed')
    expected = {**intent, **{key: config[key] for key in ('mode', 'device') if key in config}}
    handle = validate_launch_receipt(launch_raw, request_raw, intent=expected)
    if (not same_handle(handle, observation['handle'])
            or handle['supervisor']['code_sha256'] != hashlib.sha256(Path(__file__).with_name('npu_job_supervisor.py').read_bytes()).hexdigest()
            or config.get('remote_root') is not None and handle['remote_root'] != config['remote_root']):
        raise ValueError('Observed launch differs from original request or pinned supervisor')
    acknowledged, _ = original_acknowledgement(folder, intent)
    if acknowledged is not None and not same_handle(acknowledged, handle):
        raise ValueError('Observed launch differs from original acknowledgement')
    retain_original_bytes(folder / 'observed-launch.json', launch_raw)
    retain_original_bytes(folder / 'observed-execution-request.json', request_raw)
    return handle


def unknown_observation(folder, intent, error, *, observation=None, record=None, identity_conflict=False):
    """Expose only an authenticated original handle; never settle an unknown job."""
    handle = None
    launch = folder / 'launch.json'
    if not identity_conflict:
        try:
            acknowledged, original_path = original_acknowledgement(folder, intent)
            if original_path is not None: launch = original_path
            if acknowledged is not None and (observation is None or observation.get('handle') is None
                    or same_handle(acknowledged, observation['handle'])):
                handle = wire_handle(acknowledged)
        except (KeyError, ValueError, TypeError, OSError):
            pass
    return {'status': 'unknown', 'data': {'attempt': attempt_number(intent),
            'source_sha256': intent['source_sha256'], 'remote_attempt': intent['remote_attempt'],
            'original_handle': wire_handle(handle), 'identity_verified': handle is not None,
            'original_intent': str(folder / 'intent.json'),
            'original_launch': str(launch) if launch.is_file() else None,
            'original_execution_request': str(folder / 'observed-execution-request.json')
                if (folder / 'observed-execution-request.json').is_file() else None,
            'record': str(record or folder / 'observation.json'), 'error': error}}


def execution_exit(observation, intent):
    """Validate original supervisor bytes without manufacturing benchmark output."""
    from npu_job_supervisor import validate_exit_receipt
    raw = base64.b64decode(observation['execution_exit_bytes'], validate=True)
    if hashlib.sha256(raw).hexdigest() != observation['execution_exit_sha256']:
        raise ValueError('Original supervisor exit bytes changed')
    record = validate_exit_receipt(raw, handle=observation['handle'], intent=intent)
    if record != observation['execution_exit']:
        raise ValueError('Original supervisor exit projection changed')
    return raw, record


def validate_benchmark_result(observation, intent, receipt):
    """Keep a real failed benchmark report distinct from a missing report."""
    result = observation.get('result')
    if (not isinstance(result, dict) or result.get('status') not in {'completed', 'failed'}
            or type(result.get('correct')) is not bool):
        raise ValueError('Original benchmark has no valid terminal status')
    if result.get('source_sha256') != intent['source_sha256']:
        raise ValueError('Original benchmark source identity differs')
    # Both pinned native benchmark drivers atomically write this exact format.
    # Match the adapter's independent raw receipt validation before projection.
    raw = (json.dumps(result, indent=2, allow_nan=False) + '\n').encode()
    if hashlib.sha256(raw).hexdigest() != observation.get('result_sha256'):
        raise ValueError('Original benchmark result hash changed')
    if receipt['returncode'] != 0 and (result['status'] != 'failed' or result['correct'] is not False):
        raise ValueError('Nonzero native exit conflicts with benchmark success or correctness')
    return result


def execution_failure(config, folder, intent, observation, run):
    raw, receipt = execution_exit(observation, intent)
    original = folder / 'execution-exit.json'
    retain_original_bytes(original, raw)
    code = receipt['returncode']
    message = ('Native process exited with code 0 without a benchmark result'
               if code == 0 else f'Native process exited with return code {code}'
               + (f" (signal {receipt['signal']})" if receipt['signal'] is not None else ''))
    log_record = capture_run_log(folder, intent, observation, run, remote_root=config.get('remote_root')) if observation.get('log') is not None else None
    failure = {'schema_version': 'kernelbridge.native-execution-failure/v1',
               'status': 'failed', 'correct': False, 'nonqualifying': True,
               'result_origin': 'supervisor_exit', 'failure_stage': 'process_exit',
               'source_sha256': intent['source_sha256'], 'action_id': intent['action_id'],
               'attempt': attempt_number(intent), 'original_handle': observation['handle'],
               'remote_attempt': intent['remote_attempt'], 'remote_result_sha256': None,
               'execution_exit_sha256': observation['execution_exit_sha256'],
               'execution_exit_path': str(original), 'returncode': code, 'signal': receipt['signal'],
               'error': message, 'qualification': 'Original supervisor exit only; no device correctness or timing qualification.'}
    save(folder / 'execution-failure.json', failure)
    summary = {**failure, 'operator': config['mode'], 'hypothesis': intent.get('hypothesis', ''),
               'record': str(folder / 'execution-failure.json')}
    if log_record is not None: summary['run_log'] = log_record
    save(folder / 'result.json', summary)
    save(Path(config['candidate']).parent / 'feedback.json', summary)
    return {'status': 'completed', 'data': wire_summary(summary)}


ISOLATION_FILES = {
    'isolation-samples.jsonl': 'isolation_samples',
    'isolation-receipt.json': 'isolation_receipt',
    'admission.json': 'admission',
    'execution-request.json': 'execution_request',
    'execution-exit.json': 'execution_exit',
}


def isolation_requirement(config):
    value = config.get('contract', {}).get('measurement', {}).get('device_isolation')
    if value is None:
        return None
    from npu_isolation import normalize_policy, policy_digest
    if not isinstance(value, dict) or set(value) != {'policy', 'policy_sha256'}:
        raise ValueError('Invalid configured isolation policy binding')
    policy = normalize_policy(value['policy'])
    if value['policy'] != policy or value.get('policy_sha256') != policy_digest(policy):
        raise ValueError('Configured isolation policy hash differs')
    return value


def isolation_request_fields(config):
    requirement = isolation_requirement(config)
    if requirement is None:
        return {}
    return {'isolation_policy': requirement['policy'],
            'npu_enter_code_sha256': hashlib.sha256(Path(__file__).with_name('npu_enter.py').read_bytes()).hexdigest(),
            'isolation_code_sha256': hashlib.sha256(Path(__file__).with_name('npu_isolation.py').read_bytes()).hexdigest(),
            'process_isolation_code_sha256': hashlib.sha256((Path(__file__).resolve().parents[1] / 'operator_pipeline/process_isolation.py').read_bytes()).hexdigest()}


def retain_isolation_bytes(folder, observation):
    """Retain independently hashed originals; no clean/status projection is proof."""
    for name, field in ISOLATION_FILES.items():
        if observation.get(field + '_bytes') is None:
            continue
        raw = base64.b64decode(observation[field + '_bytes'], validate=True)
        if len(raw) > 64 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != observation.get(field + '_sha256'):
            raise ValueError('Original isolation evidence bytes differ: ' + name)
        retain_original_bytes(folder / name, raw)


def isolation_documents(folder):
    documents = {}
    for name in ISOLATION_FILES:
        path = folder / name
        if path.exists() or path.is_symlink():
            if path.resolve() != path or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
                raise ValueError('Original isolation evidence is missing or linked: ' + name)
            documents[name] = path.read_bytes()
    return documents


def verify_isolation_request(config, intent, raw):
    request = json.loads(raw)
    expected = {**intent, 'attempt': attempt_number(intent),
                **{key: config[key] for key in ('mode', 'device') if key in config}}
    for key in ('action_id', 'attempt', 'source_sha256', 'mode', 'device'):
        if key in expected and request.get('intent', {}).get(key) != expected[key]:
            raise ValueError('Original isolation request identity differs: ' + key)
    for key, value in {**isolation_request_fields(config),
                       'remote_root': config['remote_root'], 'remote_attempt': intent['remote_attempt'],
                       'supervisor_sha256': hashlib.sha256(Path(__file__).with_name('npu_job_supervisor.py').read_bytes()).hexdigest()}.items():
        if request.get(key) != value:
            raise ValueError('Original isolation request binding differs: ' + key)
    return expected


def verified_admission(config, folder, intent):
    requirement = isolation_requirement(config)
    if requirement is None:
        raise ValueError('No isolation admission policy was configured')
    from npu_isolation import validate_admission
    documents = isolation_documents(folder)
    expected = verify_isolation_request(config, intent, documents['execution-request.json'])
    if (folder / 'launch.json').is_file() and json.loads((folder / 'launch.json').read_bytes()).get('handle') is not None:
        raise ValueError('Not-started admission conflicts with an acknowledged process')
    if (folder / 'observed-launch.json').exists() or (folder / 'execution-exit.json').exists():
        raise ValueError('Not-started admission conflicts with an original process receipt')
    receipt = validate_admission(documents['admission.json'], documents['isolation-samples.jsonl'],
                                 documents['execution-request.json'], intent=expected, policy=requirement['policy'])
    return receipt, documents


def admission_outcome(config, folder, intent):
    receipt, documents = verified_admission(config, folder, intent)
    return {'status': 'not_started', 'data': {
        'attempt': attempt_number(intent), 'action_id': intent['action_id'],
        'source_sha256': intent['source_sha256'], 'remote_attempt': intent['remote_attempt'],
        'original_handle': None, 'dispatch_outcome': 'not_started', 'admission': receipt,
        'artifacts': {name: hashlib.sha256(raw).hexdigest() for name, raw in documents.items()},
        'record': str(folder / 'admission.json')}}


def isolation_projection(config, folder, intent, handle):
    requirement = isolation_requirement(config)
    if requirement is None:
        return None
    from npu_isolation import validate_evidence
    descriptor = {'schema_version': 'kernelbridge.device-isolation-evidence/v1',
                  'policy_sha256': requirement['policy_sha256'], 'artifacts': {}}
    try:
        documents = isolation_documents(folder)
        descriptor['artifacts'] = {name: hashlib.sha256(raw).hexdigest() for name, raw in documents.items()
                                   if name != 'admission.json'}
        expected = verify_isolation_request(config, intent, documents['execution-request.json'])
        derived = validate_evidence(documents['isolation-samples.jsonl'], documents['isolation-receipt.json'],
            documents['execution-request.json'], documents['execution-exit.json'],
            handle=handle, intent=expected, policy=requirement['policy'])
        observation = json.loads((folder / 'observation.json').read_bytes())
        receipt = json.loads(documents['isolation-receipt.json'])
        if receipt.get('result_sha256') != observation.get('result_sha256'):
            raise ValueError('Isolation receipt differs from the original benchmark result')
    except (KeyError, ValueError, TypeError, OSError) as error:
        derived = {'verdict': 'unknown', 'reason': str(error)}
    return {**descriptor, 'verdict': derived['verdict'],
            **({'reason': str(derived['reason'])[:2000]} if derived.get('reason') else {}),
            **({'structured_reasons': derived['structured_reasons'],
                'omitted_reasons': derived.get('omitted_reasons', 0)} if derived.get('structured_reasons') else {})}


def repeatable_attempt(config, folder, intent):
    """Only proven no-effect or settled nonqualifying observation admits retest."""
    if (folder / 'admission.json').is_file():
        verified_admission(config, folder, intent)
        return True
    if isolation_requirement(config) is None or not (folder / 'result.json').is_file():
        return False
    observation = json.loads((folder / 'observation.json').read_bytes())
    retain_observed_launch(config, folder, intent, observation)
    raw, _ = execution_exit(observation, intent)
    if (folder / 'execution-exit.json').read_bytes() != raw:
        raise ValueError('Retained original exit changed before retest')
    proof = isolation_projection(config, folder, intent, observation['handle'])
    return proof['verdict'] in {'foreign', 'unknown'}


def collect(config, folder, run):
    intent = json.loads((folder / 'intent.json').read_text())
    deadline = time.monotonic() + config.get('wait_seconds', 900)
    while True:
        try:
            observation = invoke_remote(run, COLLECT, intent['remote_attempt'], '--expected',
                                        body=observation_request(config, folder, intent))
        except Exception as error:
            save(folder / 'collection-uncertain.json', {'error': str(error), 'remote_attempt': intent['remote_attempt']})
            return unknown_observation(folder, intent, 'Could not observe the original job; recover this same attempt',
                                       record=folder / 'collection-uncertain.json')
        save(folder / 'observation.json', observation)
        if observation.get('lifecycle_state') == 'not_started' or observation.get('status') == 'not_started':
            try:
                retain_isolation_bytes(folder, observation)
                return admission_outcome(config, folder, intent)
            except (KeyError, ValueError, TypeError, OSError) as error:
                return unknown_observation(folder, intent, 'Original admission evidence is invalid: ' + str(error),
                                           observation=observation, identity_conflict=True)
        launch = folder / 'launch.json'
        acknowledged = json.loads(launch.read_text()).get('handle') if launch.is_file() else None
        if acknowledged is not None and not same_handle(acknowledged, observation.get('handle')):
            return unknown_observation(folder, intent, 'Observed handle differs from the original launch acknowledgement',
                                       observation=observation, identity_conflict=True)
        supervised = 'lifecycle_state' in observation or (isinstance(observation.get('handle'), dict) and 'supervisor' in observation['handle'])
        if supervised:
            if observation.get('identity_verified') is not True:
                return unknown_observation(folder, intent, 'Original supervisor identity could not be verified',
                                           observation=observation, identity_conflict=True)
            try:
                retain_observed_launch(config, folder, intent, observation)
            except (KeyError, ValueError, TypeError, OSError) as error:
                return unknown_observation(folder, intent, 'Original observed launch is invalid: ' + str(error),
                                           observation=observation, identity_conflict=True)
            if observation.get('execution_exit') is not None:
                try:
                    raw, receipt = execution_exit(observation, intent)
                    retain_original_bytes(folder / 'execution-exit.json', raw)
                except (KeyError, ValueError, TypeError) as error:
                    return unknown_observation(folder, intent, 'Original supervisor exit is invalid: ' + str(error),
                                               observation=observation, identity_conflict=True)
                if (isolation_requirement(config) is not None
                        and observation.get('isolation_receipt_bytes') is None
                        and observation.get('supervisor_alive') is True and time.monotonic() < deadline):
                    time.sleep(config.get('poll_seconds', 5))
                    continue
                if isolation_requirement(config) is not None:
                    try:
                        retain_isolation_bytes(folder, observation)
                    except (KeyError, ValueError, TypeError, OSError) as error:
                        save(folder / 'isolation-collection-error.json', {'error': str(error)})
                if observation['result'] is None:
                    return execution_failure(config, folder, intent, observation, run)
                try:
                    validate_benchmark_result(observation, intent, receipt)
                except (ValueError, TypeError) as error:
                    return unknown_observation(folder, intent, 'Original benchmark result is invalid: ' + str(error),
                                               observation=observation)
            else:
                awaiting_original_exit = observation.get('supervisor_alive') is True and (
                    observation.get('lifecycle_state') == 'running' and observation.get('alive') is True
                    or observation.get('lifecycle_state') == 'settling' and observation.get('alive') is False)
                if not awaiting_original_exit or time.monotonic() >= deadline:
                    return unknown_observation(folder, intent, 'Original supervised job has no verified exit receipt; recover its existing identity', observation=observation)
                time.sleep(config.get('poll_seconds', 5))
                continue
        if observation['result'] is not None:
            result = observation['result']
            if result.get('source_sha256') != intent['source_sha256']:
                raise ValueError('Original result source identity differs')
            log_record = capture_run_log(folder, intent, observation, run, remote_root=config.get('remote_root')) if observation.get('log') is not None else None
            if observation.get('cpu_fallback') or 'fall back to run on the CPU' in observation['log_tail'] or 'npu_cpu_fallback' in observation['log_tail']:
                result = {**result, 'status': 'failed', 'correct': False, 'message': 'CPU fallback detected; not qualified NPU timing'}
            save(folder / 'device-result.json', result)
            summary = {'attempt': intent.get('attempt', intent.get('round')), 'operator': config['mode'], 'source_sha256': intent['source_sha256'],
                       'hypothesis': intent['hypothesis'], 'status': result['status'], 'correct': result.get('correct', False),
                       'speedup': result.get('geomean_speedup'), 'baseline': result.get('baseline'),
                       'cases': [{key: row[key] for key in ('id','correct','candidate','eager','speedup','input_sha256')} for row in result.get('cases', [])],
                       'error': result.get('message'), 'remote_attempt': intent['remote_attempt'],
                       'original_handle': observation['handle'], 'remote_result_sha256': observation['result_sha256'],
                       'record': str(folder / 'device-result.json'), 'diagnostic': observation['log_tail'] if result['status'] != 'completed' else None,
                       'diagnostics': result.get('diagnostics'),
                       'promotion': 'none; compare rounds as best-observed only',
                       **({'measurement_retry_of': intent['measurement_retry_of']} if 'measurement_retry_of' in intent else {})}
            if log_record is not None:
                summary['run_log'] = {**log_record, 'original_handle': wire_handle(log_record['original_handle'])}
            if observation.get('execution_exit_sha256') is not None:
                summary.update(result_origin='benchmark', execution_exit_sha256=observation['execution_exit_sha256'],
                               execution_exit_path=str(folder / 'execution-exit.json'))
            proof = isolation_projection(config, folder, intent, observation['handle'])
            if proof is not None:
                summary['device_isolation'] = proof
            save(folder / 'result.json', summary)
            save(Path(config['candidate']).parent / 'feedback.json', summary)
            return {'status': 'completed', 'data': wire_summary(summary)}
        if not observation['alive']:
            return unknown_observation(folder, intent, 'Original remote process lacks a final result; inspect its handle and log, never redispatch', observation=observation)
        if time.monotonic() >= deadline:
            return unknown_observation(folder, intent, 'Original device job is still running; recover this same attempt', observation=observation)
        time.sleep(config.get('poll_seconds', 5))


def attempt_records(state):
    return sorted([*state.glob('attempt-*/intent.json'), *state.glob('round-*/intent.json')])


def attempt_number(intent):
    return intent.get('attempt', intent.get('round'))


def trial(config, action=None, recover=None):
    state = Path(config['state']).resolve(); state.mkdir(parents=True, exist_ok=True)
    with (state / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior_records = [(path, json.loads(path.read_text())) for path in attempt_records(state)]
        if recover is not None:
            matches = [path.parent for path, intent in prior_records if attempt_number(intent) == recover]
            if len(matches) != 1: raise ValueError('Recovery requires one original attempt number')
            folder = matches[0]
            if (folder / 'admission.json').is_file():
                return admission_outcome(config, folder, json.loads((folder / 'intent.json').read_bytes()))
            return collect(config, folder, transport(config))
        if not isinstance(action.get('id'), str) or not action['id']:
            raise ValueError('Original tool action ID is required')
        matches = [path.parent for path, intent in prior_records if intent['action_id'] == action['id']]
        if len(matches) > 1: raise ValueError('Ambiguous original action identity')
        if matches:
            folder = matches[0]
            if (folder / 'admission.json').is_file():
                return admission_outcome(config, folder, json.loads((folder / 'intent.json').read_bytes()))
            if (folder / 'result.json').exists(): return {'status': 'completed', 'data': wire_summary(json.loads((folder / 'result.json').read_text()))}
            return collect(config, folder, transport(config))
        for path, prior_intent in prior_records:
            if not (path.parent / 'result.json').is_file() and not repeatable_attempt(config, path.parent, prior_intent):
                return {'status': 'unknown', 'data': {'error': 'An earlier original attempt has no final receipt; recover it before starting another attempt', 'record': str(path)}}
        candidate = Path(config['candidate']).resolve(); parsed = sources(candidate)
        data = candidate.read_bytes(); source_sha = hashlib.sha256(data).hexdigest()
        implementation_sha = implementation_digest(parsed)
        retry_of = []
        for path, intent in prior_records:
            previous = path.parent / 'candidate.py'
            if previous.exists() and implementation_digest(sources(previous)) == implementation_sha:
                if not repeatable_attempt(config, path.parent, intent):
                    raise ValueError('Native implementation already evaluated; recover its original receipt instead of relaunching')
                retry_of.append({'attempt': attempt_number(intent), 'action_id': intent['action_id'],
                    'source_sha256': intent['source_sha256'], 'same_implementation': True,
                    'reason': 'not_started' if (path.parent / 'admission.json').is_file() else 'nonqualifying_device_isolation',
                    'original_evidence': {name: hashlib.sha256(raw).hexdigest()
                        for name, raw in isolation_documents(path.parent).items()}})
        namespace = config['attempt_namespace']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', namespace): raise ValueError('attempt_namespace must be one path component')
        benchmark = config.get('benchmark_script', 'npu_benchmark.py')
        if not re.fullmatch(r'npu_[A-Za-z0-9_]+[.]py', benchmark): raise ValueError('Expected a task-owned NPU benchmark filename')
        run = transport(config)
        number = max((attempt_number(intent) for _, intent in prior_records), default=0) + 1
        folder = state / f'attempt-{number:04d}'; folder.mkdir()
        intent = {'attempt': number, 'action_id': action['id'], 'input_id': action.get('input_id'),
                  'source_sha256': source_sha, 'implementation_sha256': implementation_sha,
                  'hypothesis': action.get('arguments', {}).get('hypothesis', ''), 'started_ns': time.time_ns(),
                  **({'measurement_retry_of': retry_of} if retry_of else {}),
                  'remote_attempt': config['remote_root'].rstrip('/') + '/' + namespace + '/' + config['mode'] + f'/{number:04d}-' + source_sha[:16]}
        save(folder / 'intent.json', intent); (folder / 'candidate.py').write_bytes(data)
        request = {**intent, 'source': base64.b64encode(data).decode(), 'mode': config['mode'], 'device': config.get('device', 0),
                   'remote_root': config['remote_root'], 'benchmark_script': benchmark,
                   'supervisor_sha256': hashlib.sha256(Path(__file__).with_name('npu_job_supervisor.py').read_bytes()).hexdigest(),
                   'warmup': config.get('warmup', 3), 'repeats': config.get('repeats', 10), 'pairs': config.get('pairs', 3),
                   'profile': config.get('profile', False) or number in config.get('profile_attempts', [1]),
                   **isolation_request_fields(config)}
        try:
            acknowledgement = invoke_remote(run, LAUNCH, body=request)
            save(folder / 'launch.json', acknowledgement)
            if acknowledgement.get('status') == 'not_started':
                retain_isolation_bytes(folder, acknowledgement)
                return admission_outcome(config, folder, intent)
        except Exception as error:
            save(folder / 'launch-uncertain.json', {'error': str(error), 'remote_attempt': intent['remote_attempt']})
        return collect(config, folder, run)


if __name__ == '__main__':
    try:
        config = json.loads(Path(sys.argv[1]).read_text())
        result = trial(config, recover=int(sys.argv[3])) if len(sys.argv) == 4 and sys.argv[2] == '--recover' else trial(config, json.load(sys.stdin))
    except Exception as error:
        result = {'status': 'failed', 'data': {'error': type(error).__name__, 'message': str(error)}}
    print(json.dumps(result, allow_nan=False))
