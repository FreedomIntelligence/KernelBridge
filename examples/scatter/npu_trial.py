"""Business-tool adapter for a frozen NPU attempt; reconnects to the original job.

argv: python -B npu_trial.py TRIAL_CONFIG.json; stdin: original Action JSON.
Recovery-only: add --recover ROUND (never launches a replacement job).
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
r=json.load(sys.stdin); folder=Path(r['remote_attempt']); original={k:r[k] for k in ('action_id','round','source_sha256','mode','device')}
if folder.exists():
 old=json.loads((folder/'intent.json').read_text())
 if old != original:raise ValueError('Existing remote attempt belongs to different input')
 print(json.dumps({'existing':True,'handle':json.loads((folder/'launch.json').read_text()) if (folder/'launch.json').exists() else None}));sys.exit()
folder.mkdir(parents=True,exist_ok=False)
(folder/'intent.json').write_text(json.dumps(original))
data=base64.b64decode(r['source']);assert hashlib.sha256(data).hexdigest()==r['source_sha256']
(folder/'candidate.py').write_bytes(data)
root=Path(r['remote_root'])
argv=['python3',str(root/'tools/npu_enter.py'),str(root/'profile.json'),str(root/'tools/npu_benchmark.py'),'--candidate',str(folder/'candidate.py'),'--output',str(folder/'job'),'--mode',r['mode'],'--device',str(r['device']),'--warmup',str(r['warmup']),'--repeats',str(r['repeats']),'--pairs',str(r['pairs'])]
if r.get('profile'):argv.append('--profile')
with (folder/'run.log').open('xb') as log:p=subprocess.Popen(argv,cwd=root,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
handle={'pid':p.pid,'argv':argv,'started_ns':time.time_ns(),'attempt':str(folder)}
temp=folder/'launch.tmp';temp.write_text(json.dumps(handle));temp.replace(folder/'launch.json')
print(json.dumps({'existing':False,'handle':handle}))
'''

COLLECT = r'''import json,sys,hashlib
from pathlib import Path
folder=Path(sys.argv[1]); path=folder/'job/result.json'; handle_path=folder/'launch.json'
handle=json.loads(handle_path.read_text()) if handle_path.exists() else None
proc=Path('/proc')/str(handle['pid']) if handle else None
alive=bool(proc and (proc/'cmdline').exists() and b'npu_benchmark.py' in (proc/'cmdline').read_bytes())
result=json.loads(path.read_text()) if path.exists() else None
log=folder/'run.log'
markers=(b'fall back to run on the CPU',b'npu_cpu_fallback')
overlap=b''; tail=b''; cpu_fallback=False
if log.exists():
 with log.open('rb') as stream:
  while chunk:=stream.read(65536):
   scanned=overlap+chunk
   cpu_fallback=cpu_fallback or any(marker in scanned for marker in markers)
   overlap=scanned[-(max(map(len,markers))-1):]
   tail=(tail+chunk)[-12000:]
print(json.dumps({'handle':handle,'alive':alive,'result':result,'result_sha256':hashlib.sha256(path.read_bytes()).hexdigest() if result else None,'cpu_fallback':cpu_fallback,'log_tail':tail.decode(errors='replace')}))
'''


def transport(config):
    spec = importlib.util.spec_from_file_location('task_npu_transport', config['transport_module'])
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.run


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
            return {'status': 'unknown', 'data': {'round': intent['round'], 'error': 'Could not observe the original job; recover this same attempt', 'record': str(folder / 'collection-uncertain.json')}}
        save(folder / 'observation.json', observation)
        if observation['result'] is not None:
            result = observation['result']
            if result.get('source_sha256') != intent['source_sha256']:
                raise ValueError('Original result source identity differs')
            if observation.get('cpu_fallback') or 'fall back to run on the CPU' in observation['log_tail'] or 'npu_cpu_fallback' in observation['log_tail']:
                result = {**result, 'status': 'failed', 'correct': False, 'message': 'CPU fallback detected; not qualified NPU timing'}
            save(folder / 'device-result.json', result)
            summary = {'round': intent['round'], 'operator': config['mode'], 'source_sha256': intent['source_sha256'],
                       'hypothesis': intent['hypothesis'], 'status': result['status'], 'correct': result.get('correct', False),
                       'speedup': result.get('geomean_speedup'), 'baseline': result.get('baseline'),
                       'cases': [{key: row[key] for key in ('id','correct','candidate','eager','speedup','input_sha256')} for row in result.get('cases', [])],
                       'error': result.get('message'), 'remote_attempt': intent['remote_attempt'],
                       'original_handle': observation['handle'], 'remote_result_sha256': observation['result_sha256'],
                       'record': str(folder / 'device-result.json'), 'diagnostic': observation['log_tail'] if result['status'] != 'completed' else None,
                       'diagnostics': result.get('diagnostics'),
                       'promotion': 'none; compare rounds as best-observed only'}
            save(folder / 'result.json', summary)
            save(Path(config['candidate']).parent / 'feedback.json', summary)
            return {'status': 'completed', 'data': summary}
        if not observation['alive']:
            return {'status': 'unknown', 'data': {'round': intent['round'], 'error': 'Original remote process lacks a final result; inspect its handle and log, never redispatch', 'record': str(folder / 'observation.json')}}
        if time.monotonic() >= deadline:
            return {'status': 'unknown', 'data': {'round': intent['round'], 'error': 'Original device job is still running; recover this same attempt', 'record': str(folder / 'observation.json')}}
        time.sleep(config.get('poll_seconds', 5))


def trial(config, action=None, recover=None):
    state = Path(config['state']).resolve(); state.mkdir(parents=True, exist_ok=True)
    with (state / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        number = recover if recover is not None else action['arguments']['round']
        if type(number) is not int or not 1 <= number <= 20: raise ValueError('round must be 1..20')
        folder = state / f'round-{number:02d}'
        run = transport(config)
        if recover is not None:
            return collect(config, folder, run)
        requested = json.loads((state / 'requested.json').read_text())
        if requested['round'] != number: raise ValueError('Only the coordinator-requested round is authorized')
        if folder.exists():
            old = json.loads((folder / 'intent.json').read_text())
            if old['action_id'] != action['id']: raise ValueError('Round already has an original action; use recovery, do not launch again')
            if (folder / 'result.json').exists(): return {'status': 'completed', 'data': json.loads((folder / 'result.json').read_text())}
            return collect(config, folder, run)
        for prior in state.glob('round-*/intent.json'):
            if not (prior.parent / 'result.json').is_file():
                return {'status': 'unknown', 'data': {'error': 'An earlier original attempt has no final receipt; recover it before starting another round', 'record': str(prior)}}
        candidate = Path(config['candidate']).resolve(); parsed = sources(candidate)
        data = candidate.read_bytes(); sha = hashlib.sha256(data).hexdigest()
        implementation_sha = implementation_digest(parsed)
        if config.get('initial_candidate') and implementation_digest(sources(Path(config['initial_candidate']))) == implementation_sha:
            raise ValueError('Unmodified initial implementation does not count as an optimization round')
        for prior in state.glob('round-*/candidate.py'):
            if implementation_digest(sources(prior)) == implementation_sha:
                raise ValueError('Native implementation already evaluated; comments, whitespace, model wrapper and metadata do not count as a new optimization')
        namespace = config.get('attempt_namespace', 'rounds')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', namespace):
            raise ValueError('attempt_namespace must be one path component')
        folder.mkdir()
        intent = {'round': number, 'action_id': action['id'], 'input_id': requested['input_id'],
                  'source_sha256': sha, 'implementation_sha256': implementation_sha, 'hypothesis': action['arguments']['hypothesis'], 'started_ns': time.time_ns(),
                  'remote_attempt': config['remote_root'].rstrip('/') + '/' + namespace + '/' + config['mode'] + f'/{number:02d}-' + sha[:16]}
        save(folder / 'intent.json', intent); (folder / 'candidate.py').write_bytes(data)
        request = {**intent, 'source': base64.b64encode(data).decode(), 'mode': config['mode'], 'device': config.get('device', 0),
                   'remote_root': config['remote_root'], 'warmup': config.get('warmup', 3), 'repeats': config.get('repeats', 10),
                   'pairs': config.get('pairs', 3), 'profile': number in config.get('profile_rounds', [1, 20])}
        try:
            acknowledgement = invoke_remote(run, LAUNCH, body=request)
            save(folder / 'launch.json', acknowledgement)
        except Exception as error:
            # The launch may have happened; no second launch request is issued.
            save(folder / 'launch-uncertain.json', {'error': str(error), 'remote_attempt': intent['remote_attempt']})
        return collect(config, folder, run)


if __name__ == '__main__':
    try:
        config = json.loads(Path(sys.argv[1]).read_text())
        result = trial(config, recover=int(sys.argv[3])) if len(sys.argv) == 4 and sys.argv[2] == '--recover' else trial(config, json.load(sys.stdin))
    except Exception as error:
        result = {'status': 'failed', 'data': {'error': type(error).__name__, 'message': str(error)}}
    print(json.dumps(result, allow_nan=False))
