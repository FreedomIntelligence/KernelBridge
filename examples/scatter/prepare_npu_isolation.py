"""Initialize a source-bound NPU process-view profile with one context probe.

prepare writes local inputs only. initialize prepares, dispatches once, then
performs bounded read-only recovery. After any uncertain dispatch, recover the
original output directory; never dispatch a replacement probe automatically.
The probe allocates one byte and holds a context; it does not measure an operator.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
SCHEMA = 'kernelbridge.pid-view-probe-request/v2'
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_bytes(path):
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError('A pinned input or original artifact is linked or nonregular')
    return path.read_bytes()


def save_bytes(path, raw):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temporary.unlink()


def save(path, value):
    save_bytes(path, canonical(value) + b'\n')


def load_module(path, expected):
    raw = read_bytes(path)
    if digest(raw) != expected: raise ValueError('Pinned initialization code changed')
    name = 'npu_profile_' + path.stem + '_' + expected
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


def prepare(args):
    output = args.output.absolute()
    if output.exists() or output.resolve() != output:
        raise ValueError('Initialization requires a new canonical output directory')
    profile_path = args.profile.resolve(strict=True)
    profile_raw = read_bytes(profile_path); profile = json.loads(profile_raw)
    for name in ('sdk', 'python'):
        if not isinstance(profile.get(name), str) or not Path(profile[name]).is_absolute() or '..' in Path(profile[name]).parts:
            raise ValueError('Input profile must declare absolute remote sdk and python paths')
    if type(args.device) is not int or args.device < 0:
        raise ValueError('Select a nonnegative logical device explicitly')
    remote_root = Path(args.remote_root)
    if not remote_root.is_absolute() or '..' in remote_root.parts:
        raise ValueError('Select an authorized absolute remote root without traversal')
    transport_path = args.transport.resolve(strict=True)
    transport_pins = {str(path): digest(read_bytes(path)) for path in
                      (transport_path, *(item.resolve(strict=True) for item in args.transport_dependency))}
    helper = HERE / 'process_isolation.py'
    if not helper.exists(): helper = HERE.parent / 'operator_pipeline/process_isolation.py'
    paths = {'tools/npu_enter.py': HERE / 'npu_enter.py', 'tools/npu_isolation.py': HERE / 'npu_isolation.py',
             'tools/process_isolation.py': helper, 'npu_pid_view.py': HERE / 'npu_pid_view.py'}
    payload = {name: read_bytes(path) for name, path in paths.items()}
    payload['profile.json'] = canonical({name: profile[name] for name in ('sdk', 'python', 'executor_source', 'cmake_toolchain', 'python_include') if name in profile}) + b'\n'
    isolation = load_module(paths['tools/npu_isolation.py'], digest(payload['tools/npu_isolation.py']))
    declared = profile.get('device_isolation', {})
    policy = dict(declared.get('policy', declared))
    policy.pop('view_evidence', None)
    policy['logical_device'] = args.device
    if args.lease_root is not None: policy['lease_root'] = args.lease_root
    policy = isolation.normalize_policy(policy)
    probe_id = 'pid-view-' + uuid.uuid4().hex
    request = {'schema_version': SCHEMA, 'probe_id': probe_id, 'remote_root': str(remote_root),
               'remote_probe': str(remote_root / '.infrastructure-probes' / probe_id),
               'isolation_policy': policy, 'hold_seconds': 12,
               'allocation': {'elements': 1, 'dtype': 'uint8', 'algorithm': None},
               'required_files': {name: digest(raw) for name, raw in payload.items() if name != 'npu_pid_view.py'},
               'receiver_sha256': digest(payload['npu_pid_view.py'])}
    output.mkdir(parents=True, mode=0o700)
    (output / 'tools').mkdir(mode=0o700)
    for name, raw in payload.items(): save_bytes(output / name, raw)
    save_bytes(output / 'tools/prepare_npu_isolation.py', read_bytes(Path(__file__).resolve()))
    save_bytes(output / 'input-profile.json', profile_raw)
    save(output / 'request.json', request)
    save(output / 'prepared.json', {'schema_version': 'kernelbridge.pid-view-probe-prepared/v1',
        'request_sha256': digest(read_bytes(output / 'request.json')), 'transport_module': str(transport_path),
        'transport_code_pins': transport_pins, 'input_profile': str(profile_path),
        'input_profile_sha256': digest(profile_raw), 'client_sha256': digest(read_bytes(Path(__file__).resolve())),
        'executed': False, 'performance_claim': None})
    return {'status': 'prepared', 'output': str(output), 'probe_id': probe_id,
            'next': ['dispatch', '--output', str(output)]}


# Only the unique infrastructure namespace is written. The authorized root's
# existing deployments, profile, tools, attempts and device locks are untouched.
STAGE = r'''
import base64,hashlib,json,os,pathlib,re,stat,subprocess,sys
v=json.loads(sys.stdin.buffer.read());raw=base64.b64decode(v['request'],validate=True);r=json.loads(raw)
sha=lambda b:hashlib.sha256(b).hexdigest()
if sha(raw)!=v['request_sha256'] or r['schema_version']!='kernelbridge.pid-view-probe-request/v2':raise ValueError('Request changed')
declared=pathlib.Path(r['remote_root'])
if not declared.is_absolute() or '..' in declared.parts:raise ValueError('Invalid authorized root')
root=declared.resolve(strict=True)
if not root.is_dir():raise ValueError('Authorized root is not a directory')
if re.fullmatch(r'pid-view-[0-9a-f]{32}',r['probe_id']) is None or r['remote_probe']!=str(declared/'.infrastructure-probes'/r['probe_id']):raise ValueError('Probe namespace changed')
parent=root/'.infrastructure-probes';parent.mkdir(mode=0o700,exist_ok=True)
if parent.resolve()!=parent:raise ValueError('Probe parent is linked')
folder=parent/r['probe_id'];folder.mkdir(mode=0o700,exist_ok=False);(folder/'tools').mkdir(mode=0o700)
expected={**r['required_files'],'npu_pid_view.py':r['receiver_sha256']}
if set(v['files'])!=set(expected) or set(expected)!={'profile.json','npu_pid_view.py','tools/npu_enter.py','tools/npu_isolation.py','tools/process_isolation.py'}:raise ValueError('Unexpected payload closure')
def write(name,data):
 fd=os.open(folder/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
for name,expected_sha in expected.items():
 data=base64.b64decode(v['files'][name],validate=True)
 if sha(data)!=expected_sha:raise ValueError('Pinned payload changed')
 write(name,data)
write('request.json',raw)
write('dispatch-intent.json',json.dumps({'request_sha256':sha(raw),'receiver_sha256':r['receiver_sha256']}).encode())
directory=os.open(folder,os.O_RDONLY)
try:os.fsync(directory)
finally:os.close(directory)
env=dict(os.environ)
for key in ('KERNELBRIDGE_ISOLATION_REQUEST_PATH','KERNELBRIDGE_ISOLATION_REQUEST_SHA256'):env.pop(key,None)
with (folder/'controller.log').open('xb') as log:
 child=subprocess.Popen(['python3','-B',str(folder/'tools/npu_enter.py'),str(folder/'profile.json'),str(folder/'npu_pid_view.py'),'controller',str(folder/'request.json')],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env=env)
ack={'request_sha256':sha(raw),'controller_pid':child.pid,'remote_probe':r['remote_probe'],'identity_status':'PID is provisional until exact controller-start receipt'}
write('dispatch-ack.json',json.dumps(ack).encode());print(json.dumps(ack))
'''


COLLECT = r'''
import base64,hashlib,json,pathlib,re,sys
v=json.loads(sys.stdin.buffer.read());r=v['request'];root=pathlib.Path(r['remote_root']).resolve(strict=True)
if re.fullmatch(r'pid-view-[0-9a-f]{32}',r['probe_id']) is None or r['remote_probe']!=str(pathlib.Path(r['remote_root'])/'.infrastructure-probes'/r['probe_id']):raise ValueError('Original namespace changed')
p=root/'.infrastructure-probes'/r['probe_id']
if not p.exists():print(json.dumps({'status':'unknown','reason':'original_namespace_absent','artifacts':[]}));raise SystemExit(0)
if p.resolve()!=p:raise ValueError('Original namespace is linked')
request=p/'request.json'
if request.is_symlink() or hashlib.sha256(request.read_bytes()).hexdigest()!=v['request_sha256']:raise ValueError('Original request changed')
records=[];total=0
for child in sorted(p.iterdir()):
 if child.suffix not in ('.json','.log'):continue
 if child.is_symlink() or not child.is_file():raise ValueError('Original artifact is linked or nonregular')
 raw=child.read_bytes();total+=len(raw)
 if len(raw)>8*1024*1024 or total>32*1024*1024:raise ValueError('Original proof exceeds bounded recovery size')
 records.append({'name':child.name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'base64':base64.b64encode(raw).decode()})
for name in (*r['required_files'],'npu_pid_view.py'):
 child=p/name
 if child.resolve()!=child or not child.is_file():raise ValueError('Original payload is linked or missing')
 raw=child.read_bytes();total+=len(raw)
 if len(raw)>8*1024*1024 or total>32*1024*1024:raise ValueError('Original proof exceeds bounded recovery size')
 if name not in [record['name'] for record in records]:records.append({'name':name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'base64':base64.b64encode(raw).decode()})
print(json.dumps({'status':'observed','remote_probe':r['remote_probe'],'artifacts':records}))
'''


def original(output):
    folder = output.resolve(strict=True)
    raw = read_bytes(folder / 'request.json'); request = json.loads(raw)
    prepared = json.loads(read_bytes(folder / 'prepared.json'))
    if digest(raw) != prepared['request_sha256']:
        raise ValueError('Original initialization request changed')
    if digest(read_bytes(Path(__file__).resolve())) != prepared['client_sha256']:
        raise ValueError('Use the retained original tools/prepare_npu_isolation.py to recover this request')
    for name, expected in {**request['required_files'], 'npu_pid_view.py': request['receiver_sha256']}.items():
        if digest(read_bytes(folder / name)) != expected: raise ValueError('Original initialization payload changed')
    return folder, raw, request, prepared


def invoke(folder, prepared, action, source, body):
    for path, expected in prepared['transport_code_pins'].items():
        if digest(read_bytes(Path(path))) != expected: raise ValueError('Pinned transport source changed')
    transport_path = Path(prepared['transport_module'])
    module = load_module(transport_path, prepared['transport_code_pins'][str(transport_path)])
    stamp = str(time.time_ns())
    try:
        reply = module.run(['python3', '-B', '-c', source], stdin=canonical(body), timeout=45)
        saved = {key: value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value for key, value in reply.items()}
        save(folder / (action + '-reply-' + stamp + '.json'), saved)
        if reply['returncode'] != 0: return {'status': 'unknown', 'reason': 'original_transport_returned_error'}
        return json.loads(reply['stdout'])
    except Exception as error:
        save(folder / (action + '-uncertain-' + stamp + '.json'), {'exception_type': type(error).__name__,
            'request_sha256': prepared['request_sha256'], 'next': 'recover original request only'})
        return {'status': 'unknown', 'reason': 'original_transport_uncertain'}


def dispatch(output):
    folder, raw, request, prepared = original(output)
    # An uncertain send must remain irrevocable, including failures before ACK.
    save(folder / 'dispatch-intent.json', {'request_sha256': digest(raw), 'created_ns': str(time.time_ns()),
        'rule': 'dispatch exactly once; subsequent operations recover this request only'})
    files = {name: base64.b64encode(read_bytes(folder / name)).decode()
             for name in (*request['required_files'], 'npu_pid_view.py')}
    reply = invoke(folder, prepared, 'dispatch', STAGE, {'request': base64.b64encode(raw).decode(),
        'request_sha256': digest(raw), 'files': files})
    return {'status': 'dispatched' if reply.get('request_sha256') == digest(raw) else 'unknown',
            'probe_id': request['probe_id'], 'output': str(folder), 'next': ['recover', '--output', str(folder)]}


def recover(output):
    folder, raw, request, prepared = original(output)
    marker = json.loads(read_bytes(folder / 'dispatch-intent.json'))
    if marker['request_sha256'] != digest(raw): raise ValueError('Original dispatch identity changed')
    reply = invoke(folder, prepared, 'recover', COLLECT, {'request': request, 'request_sha256': digest(raw)})
    artifacts = {}; total = 0; received = None
    for entry in reply.get('artifacts', []):
        name = entry['name']; data = base64.b64decode(entry['base64'], validate=True); total += len(data)
        if (name not in request['required_files'] and Path(name).name != name or name in artifacts or len(data) != entry['bytes']
                or len(data) > MAX_ARTIFACT_BYTES or total > MAX_TOTAL_BYTES or digest(data) != entry['sha256']):
            raise ValueError('Original recovered artifact identity changed')
        artifacts[name] = data
    if artifacts:
        received = folder / ('recovered-' + str(time.time_ns())); received.mkdir(mode=0o700)
        for name, data in artifacts.items():
            target = received / name; target.parent.mkdir(exist_ok=True)
            save_bytes(target, data)
    if 'probe-result.json' not in artifacts:
        return {'status': 'unknown', 'probe_id': request['probe_id'], 'output': str(folder),
                'reason': reply.get('reason', 'original_probe_not_terminal'),
                'artifacts': str(received) if received else None, 'next': ['recover', '--output', str(folder)]}
    receiver = load_module(folder / 'npu_pid_view.py', request['receiver_sha256'])
    try:
        policy = receiver.validate_probe_evidence(raw, artifacts)
    except (ValueError, KeyError, TypeError, OSError) as error:
        return {'status': 'unqualified', 'probe_id': request['probe_id'], 'output': str(folder),
                'reason': str(error)[:2048], 'artifacts': str(received), 'profile': None}
    profile_raw = read_bytes(folder / 'input-profile.json')
    if digest(profile_raw) != prepared['input_profile_sha256']: raise ValueError('Original input profile changed')
    profile = json.loads(profile_raw)
    profile.update(device=request['isolation_policy']['logical_device'], transport_module=prepared['transport_module'], remote_root=request['remote_root'])
    isolation = load_module(folder / 'tools/npu_isolation.py', request['required_files']['tools/npu_isolation.py'])
    profile['device_isolation'] = {'policy': policy, 'policy_sha256': isolation.policy_digest(policy)}
    target = folder / 'initialized-profile.json'; content = canonical(profile) + b'\n'
    if target.exists():
        if read_bytes(target) != content: raise ValueError('Previously initialized profile changed')
    else: save_bytes(target, content)
    return {'status': 'completed', 'profile': str(target), 'probe_id': request['probe_id'],
            'artifacts': str(received),
            'claim': 'retained PID-view proof; each native job must freshly qualify occupancy', 'performance_claim': None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('initialize', 'prepare', 'dispatch', 'recover'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--transport', type=Path)
    parser.add_argument('--transport-dependency', type=Path, action='append', default=[])
    parser.add_argument('--remote-root')
    parser.add_argument('--device', type=int)
    parser.add_argument('--lease-root')
    parser.add_argument('--wait-seconds', type=float, default=60)
    args = parser.parse_args()
    if not 0 <= args.wait_seconds <= 120: parser.error('--wait-seconds must be between 0 and 120')
    if args.action in ('initialize', 'prepare'):
        if any(getattr(args, name) is None for name in ('profile', 'transport', 'remote_root', 'device')):
            parser.error('prepare/initialize requires --profile --transport --remote-root --device')
        result = prepare(args)
        if args.action == 'prepare': print(json.dumps(result)); return 0
    if args.action in ('initialize', 'dispatch'):
        result = dispatch(args.output)
        if args.action == 'dispatch': print(json.dumps(result)); return 0 if result['status'] == 'dispatched' else 3
    deadline = time.monotonic() + (args.wait_seconds if args.action == 'initialize' else 0)
    while True:
        result = recover(args.output)
        if result['status'] != 'unknown' or time.monotonic() >= deadline: break
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    print(json.dumps(result))
    return 0 if result['status'] == 'completed' else 3


if __name__ == '__main__': raise SystemExit(main())
