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


LAUNCH = r'''import base64,hashlib,json,os,subprocess,sys,time
from pathlib import Path
r=json.load(sys.stdin); folder=Path(r['remote_attempt']); original={k:r[k] for k in ('action_id','attempt','source_sha256','mode','device')}
if folder.exists():
 old=json.loads((folder/'intent.json').read_text())
 if old != original:raise ValueError('Existing remote attempt belongs to different input')
 print(json.dumps({'existing':True,'handle':json.loads((folder/'launch.json').read_text()) if (folder/'launch.json').exists() else None}));sys.exit()
folder.mkdir(parents=True,exist_ok=False)
(folder/'intent.json').write_text(json.dumps(original))
data=base64.b64decode(r['source']);assert hashlib.sha256(data).hexdigest()==r['source_sha256']
(folder/'candidate.py').write_bytes(data)
root=Path(r['remote_root'])
argv=['python3',str(root/'tools/npu_enter.py'),str(root/'profile.json'),str(root/'tools'/r.get('benchmark_script','npu_benchmark.py')),'--candidate',str(folder/'candidate.py'),'--output',str(folder/'job'),'--mode',r['mode'],'--device',str(r['device']),'--warmup',str(r['warmup']),'--repeats',str(r['repeats']),'--pairs',str(r['pairs'])]
if r.get('profile'):argv.append('--profile')
import fcntl
lease=(root/'.npu-job.lock').open('a')
fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
with (folder/'run.log').open('xb') as log:p=subprocess.Popen(argv,cwd=root,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,pass_fds=(lease.fileno(),))
handle={'pid':p.pid,'argv':argv,'started_ns':time.time_ns(),'attempt':str(folder)}
temp=folder/'launch.tmp';temp.write_text(json.dumps(handle));temp.replace(folder/'launch.json')
print(json.dumps({'existing':False,'handle':handle}))
'''

COLLECT = r'''import json,sys,hashlib,os,stat
from pathlib import Path
folder=Path(sys.argv[1]); path=folder/'job/result.json'; handle_path=folder/'launch.json'
handle=json.loads(handle_path.read_text()) if handle_path.exists() else None
proc=Path('/proc')/str(handle['pid']) if handle else None
alive=bool(proc and (proc/'cmdline').exists() and b'--candidate' in (proc/'cmdline').read_bytes())
result=json.loads(path.read_text()) if path.exists() else None
log=folder/'run.log'
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
print(json.dumps({'handle':handle,'alive':alive,'result':result,'result_sha256':hashlib.sha256(path.read_bytes()).hexdigest() if result else None,'cpu_fallback':cpu_fallback,'log_tail':tail.decode(errors='replace'),'log':log_record}))
'''


# Read only the observed original file, in bounded pages. No device work, log
# rewriting or replacement launch is permitted on this recovery path.
LOG_PAGE_BYTES = 256 * 1024
MAX_LOG_BYTES = 64 * 1024 * 1024
READ_RUN_LOG = r'''import base64,hashlib,json,os,stat,sys
from pathlib import Path
r=json.load(sys.stdin); folder=Path(r['remote_attempt'])
if folder.resolve()!=folder:raise ValueError('Original attempt path contains a link')
def original_file(name):
 path=folder/name
 if path.resolve()!=path or not path.is_file():raise ValueError('Original evidence missing or linked: '+name)
 return path
intent=json.loads(original_file('intent.json').read_text())
for key in ('action_id','attempt','source_sha256'):
 if intent.get(key)!=r[key]:raise ValueError('Original intent differs: '+key)
if hashlib.sha256(original_file('candidate.py').read_bytes()).hexdigest()!=r['source_sha256']:raise ValueError('Original candidate changed')
handle=json.loads(original_file('launch.json').read_text())
if handle!=r['handle'] or handle.get('attempt')!=str(folder):raise ValueError('Original handle differs')
if hashlib.sha256(original_file('job/result.json').read_bytes()).hexdigest()!=r['result_sha256']:raise ValueError('Original result changed')
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


def capture_run_log(folder, intent, observation, run):
    """Retain verified bytes without changing an original job's terminal status."""
    metadata = observation.get('log')
    binding = {'source_sha256': intent['source_sha256'], 'remote_attempt': intent['remote_attempt'],
               'original_handle': observation.get('handle'), 'remote_result_sha256': observation.get('result_sha256')}
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


def recover_run_log(folder, run):
    """Explicit log-only recovery; never replace terminal F/G measurements."""
    with (folder.parent / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        intent = json.loads((folder / 'intent.json').read_text())
        original = json.loads((folder / 'observation.json').read_text())
        record = json.loads((folder / 'run-log.json').read_text())
        try:
            observed = invoke_remote(run, COLLECT, intent['remote_attempt'])
        except Exception as error:
            record['recovery_error'] = str(error)[:2048]
            save(folder / 'run-log.json', record)
            return record
        if (observed.get('result_sha256') != original['result_sha256']
                or observed.get('result') != original['result']
                or not same_handle(observed.get('handle'), original.get('handle'))
                or observed['result'].get('source_sha256') != intent['source_sha256']):
            raise ValueError('Original result or handle changed during log-only recovery')
        record = capture_run_log(folder, intent, observed, run)
        # Preserve original result, qualification, log-tail scan and measurement
        # receipt bytes. Only the separately bound log artifact is enriched.
        save(folder / 'observation.json', {**original, 'log': observed.get('log')})
        return record


def invoke_remote(run, script, *args, body=None):
    reply = run(['python3', '-B', '-c', script, *args], stdin=b'' if body is None else json.dumps(body).encode(), timeout=45)
    if reply['returncode']:
        raise RuntimeError(reply['stderr'].decode(errors='replace')[-4000:])
    return json.loads(reply['stdout'])


def collect(config, folder, run):
    intent = json.loads((folder / 'intent.json').read_text())
    deadline = time.monotonic() + config.get('wait_seconds', 900)
    while True:
        try:
            observation = invoke_remote(run, COLLECT, intent['remote_attempt'])
        except Exception as error:
            save(folder / 'collection-uncertain.json', {'error': str(error), 'remote_attempt': intent['remote_attempt']})
            return {'status': 'unknown', 'data': {'attempt': intent.get('attempt', intent.get('round')), 'error': 'Could not observe the original job; recover this same attempt', 'record': str(folder / 'collection-uncertain.json')}}
        save(folder / 'observation.json', observation)
        if observation['result'] is not None:
            result = observation['result']
            if result.get('source_sha256') != intent['source_sha256']:
                raise ValueError('Original result source identity differs')
            log_record = capture_run_log(folder, intent, observation, run) if observation.get('log') is not None else None
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
                       'promotion': 'none; compare rounds as best-observed only'}
            if log_record is not None:
                summary['run_log'] = {**log_record, 'original_handle': wire_handle(log_record['original_handle'])}
            save(folder / 'result.json', summary)
            save(Path(config['candidate']).parent / 'feedback.json', summary)
            return {'status': 'completed', 'data': wire_summary(summary)}
        if not observation['alive']:
            return {'status': 'unknown', 'data': {'attempt': intent.get('attempt', intent.get('round')), 'error': 'Original remote process lacks a final result; inspect its handle and log, never redispatch', 'record': str(folder / 'observation.json')}}
        if time.monotonic() >= deadline:
            return {'status': 'unknown', 'data': {'attempt': intent.get('attempt', intent.get('round')), 'error': 'Original device job is still running; recover this same attempt', 'record': str(folder / 'observation.json')}}
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
        run = transport(config)
        if recover is not None:
            matches = [path.parent for path, intent in prior_records if attempt_number(intent) == recover]
            if len(matches) != 1: raise ValueError('Recovery requires one original attempt number')
            return collect(config, matches[0], run)
        if not isinstance(action.get('id'), str) or not action['id']:
            raise ValueError('Original tool action ID is required')
        matches = [path.parent for path, intent in prior_records if intent['action_id'] == action['id']]
        if len(matches) > 1: raise ValueError('Ambiguous original action identity')
        if matches:
            folder = matches[0]
            if (folder / 'result.json').exists(): return {'status': 'completed', 'data': wire_summary(json.loads((folder / 'result.json').read_text()))}
            return collect(config, folder, run)
        for path, _ in prior_records:
            if not (path.parent / 'result.json').is_file():
                return {'status': 'unknown', 'data': {'error': 'An earlier original attempt has no final receipt; recover it before starting another attempt', 'record': str(path)}}
        candidate = Path(config['candidate']).resolve(); parsed = sources(candidate)
        data = candidate.read_bytes(); source_sha = hashlib.sha256(data).hexdigest()
        implementation_sha = implementation_digest(parsed)
        for path, intent in prior_records:
            previous = path.parent / 'candidate.py'
            if previous.exists() and implementation_digest(sources(previous)) == implementation_sha:
                raise ValueError('Native implementation already evaluated; recover its original receipt instead of relaunching')
        namespace = config['attempt_namespace']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', namespace): raise ValueError('attempt_namespace must be one path component')
        benchmark = config.get('benchmark_script', 'npu_benchmark.py')
        if not re.fullmatch(r'npu_[A-Za-z0-9_]+[.]py', benchmark): raise ValueError('Expected a task-owned NPU benchmark filename')
        number = max((attempt_number(intent) for _, intent in prior_records), default=0) + 1
        folder = state / f'attempt-{number:04d}'; folder.mkdir()
        intent = {'attempt': number, 'action_id': action['id'], 'input_id': action.get('input_id'),
                  'source_sha256': source_sha, 'implementation_sha256': implementation_sha,
                  'hypothesis': action.get('arguments', {}).get('hypothesis', ''), 'started_ns': time.time_ns(),
                  'remote_attempt': config['remote_root'].rstrip('/') + '/' + namespace + '/' + config['mode'] + f'/{number:04d}-' + source_sha[:16]}
        save(folder / 'intent.json', intent); (folder / 'candidate.py').write_bytes(data)
        request = {**intent, 'source': base64.b64encode(data).decode(), 'mode': config['mode'], 'device': config.get('device', 0),
                   'remote_root': config['remote_root'], 'benchmark_script': benchmark,
                   'warmup': config.get('warmup', 3), 'repeats': config.get('repeats', 10), 'pairs': config.get('pairs', 3),
                   'profile': config.get('profile', False) or number in config.get('profile_attempts', [1])}
        try:
            acknowledgement = invoke_remote(run, LAUNCH, body=request)
            save(folder / 'launch.json', acknowledgement)
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
