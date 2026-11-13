"""Source-bound PID-view initialization for an explicitly supplied NPU profile.

The companion prepare_npu_isolation client stages this receiver and its pinned
helpers in one unique probe directory. Only an explicit dispatch starts a
minimal context holder. Recovery and evidence validation do not touch a device.
There is no candidate, algorithm, benchmark, scheduler or model API here.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

REQUEST_SCHEMA = 'kernelbridge.pid-view-probe-request/v2'
RESULT_SCHEMA = 'kernelbridge.pid-view-probe-result/v2'
PAYLOAD_FILES = ('profile.json', 'tools/npu_enter.py', 'tools/npu_isolation.py',
                 'tools/process_isolation.py')
SUCCESS_RECEIPTS = ('controller-start.json', 'before-lease.json', 'before.json',
                    'lease-admission.json', 'holder-spawn.json', 'holder-start.json',
                    'holder-ready.json', 'active.json', 'stop-holder.json',
                    'holder-returning.json', 'holder-exit.json', 'after.json',
                    'view-evidence-candidate.json')
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
NONAUTHORITATIVE_JSON = {'probe-result.json', 'dispatch-ack.json'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    raw = canonical(value) + b'\n'
    # Publish only complete bytes. A reader observing holder-ready.json must
    # never race the writer's first write. link is atomic and refuses overwrite.
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink()
    return sha(raw)


def read(path):
    return json.loads(path.read_bytes())


def _request(raw):
    """Check the portable request shape without reading runtime/proc state."""
    if not isinstance(raw, bytes) or len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError('Invalid original probe request bytes')
    request = json.loads(raw)
    if not isinstance(request, dict) or request.get('schema_version') != REQUEST_SCHEMA:
        raise ValueError('Unknown infrastructure probe request')
    probe_id = request.get('probe_id')
    if not isinstance(probe_id, str) or re.fullmatch(r'pid-view-[0-9a-f]{32}', probe_id) is None:
        raise ValueError('Invalid original probe ID')
    root = Path(request['remote_root'])
    if not root.is_absolute() or '..' in root.parts or request['remote_probe'] != str(root / '.infrastructure-probes' / probe_id):
        raise ValueError('Original probe namespace changed')
    if request.get('allocation') != {'elements': 1, 'dtype': 'uint8', 'algorithm': None} or request.get('hold_seconds') != 12:
        raise ValueError('Only the bounded one-byte context probe is supported')
    pins = request.get('required_files')
    if not isinstance(pins, dict) or set(pins) != set(PAYLOAD_FILES):
        raise ValueError('Missing exact environment/helper payload pins')
    if any(not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None
           for value in [*pins.values(), request.get('receiver_sha256')]):
        raise ValueError('Invalid probe source or payload digest')
    policy = request.get('isolation_policy')
    if not isinstance(policy, dict) or not {'logical_device', 'lease_root'} <= set(policy) or 'view_evidence' in policy:
        raise ValueError('Initialization requires an explicit device/lease policy without a pre-admitted view')
    return request


def _helpers(request):
    """Load only the exact locally staged helper bytes; no same-name imports."""
    payload = Path(__file__).resolve().parent
    if sha(Path(__file__).read_bytes()) != request['receiver_sha256']:
        raise ValueError('Original receiver source changed')
    for name in ('tools/npu_isolation.py', 'tools/process_isolation.py'):
        path = payload / name
        if path.resolve() != path or sha(path.read_bytes()) != request['required_files'][name]:
            raise ValueError('Pinned isolation payload changed: ' + name)
    path = payload / 'tools/npu_isolation.py'
    source = path.read_bytes()
    if sha(source) != request['required_files']['tools/npu_isolation.py']:
        raise ValueError('Pinned isolation source changed during loading')
    name = 'pid_view_isolation_' + request['required_files']['tools/npu_isolation.py']
    spec = importlib.util.spec_from_file_location(name, path)
    isolation = importlib.util.module_from_spec(spec)
    sys.modules[name] = isolation
    exec(compile(source, str(path), 'exec'), isolation.__dict__)
    if (isolation.SELF_SHA256 != request['required_files']['tools/npu_isolation.py']
            or isolation.processes.__source_sha256__ != request['required_files']['tools/process_isolation.py']):
        raise ValueError('Loaded isolation source differs from original payload')
    policy = isolation.normalize_policy(request['isolation_policy'])
    if policy != request['isolation_policy']:
        raise ValueError('Original isolation policy is not normalized')
    return isolation.processes, isolation, policy


def load_request(path):
    raw = path.read_bytes(); request = _request(raw)
    root = Path(request['remote_root']).resolve(strict=True)
    folder = path.parent.resolve(strict=True)
    if folder != root / '.infrastructure-probes' / request['probe_id'] or Path(__file__).resolve().parent != folder:
        raise ValueError('Original probe payload directory changed')
    for name, expected in request['required_files'].items():
        bound = folder / name
        if bound.resolve() != bound or sha(bound.read_bytes()) != expected:
            raise ValueError('Pinned probe payload changed: ' + name)
    profile = read(folder / 'profile.json')
    if Path(sys.executable).resolve() != Path(profile['python']).resolve():
        raise ValueError('Probe is outside the admitted profile Python')
    if Path(os.environ.get('ASCEND_HOME_PATH', '/missing')).resolve() != Path(profile['sdk']).resolve():
        raise ValueError('Probe is outside the admitted SDK environment')
    visibility = {key: os.environ.get(key) for key in ('ASCEND_VISIBLE_DEVICES', 'ASCEND_RT_VISIBLE_DEVICES')}
    if any(value not in (None, '') for value in visibility.values()):
        raise ValueError('Visibility remapping is not qualified by this minimal probe; do not override it')
    processes, isolation, policy = _helpers(request)
    return request, root, folder, sha(raw), processes, isolation, visibility


def raw_target(sample, request, isolation):
    # Missing view is expected for qualification. Never use sample.processes:
    # that list is unpopulated until a PID view has already been admitted.
    for name in ('mapping', 'inventory'):
        if sample[name]['returncode'] != 0:
            raise ValueError('Inventory/mapping observation failed')
    allowed_error = 'ValueError: No qualified driver PID-view proof'
    if sample.get('error') not in (None, allowed_error):
        raise ValueError('Probe sample has an unrelated observation failure: ' + str(sample['error']))
    mapping = isolation.parse_mapping(sample['mapping']['stdout'])
    inventory = isolation.parse_inventory(sample['inventory']['stdout'], mapping)
    policy = isolation.normalize_policy(request['isolation_policy'])
    target = [item for item in mapping if item['logical'] == policy['logical_device']]
    if len(target) != 1:
        raise ValueError('Fresh logical mapping has no unique requested target')
    physical_ids = policy.get('domain_physical_ids', [target[0]['physical']])
    if target[0]['physical'] not in physical_ids:
        raise ValueError('Declared isolation domain excludes the requested target')
    domain = sorted([item for item in inventory['chips'] if item['physical'] in physical_ids],
                    key=lambda item: (item['board'], item['chip']))
    if len(domain) != len(physical_ids):
        raise ValueError('Declared isolation domain has unresolved physical identities')
    if any(item.get('health') != 'OK' for item in domain):
        raise ValueError('Fresh target chip health is not OK')
    if sample['target'] != target[0] or sample['domain'] != domain:
        raise ValueError('Derived target disagrees with retained raw tables')
    selected = {(item['board'], item['chip']) for item in domain}
    pids = sorted({item['reported_pid'] for item in inventory['processes']
                   if (item['board'], item['chip']) in selected})
    return pids


def scope(sample):
    observer = sample['observer']
    return {'boot_id': observer['birth']['boot_id'], 'uid': observer['uid'],
            'pid_ns': observer['pid_ns'], 'mnt_ns': observer['mnt_ns'],
            'pid_ns_init_birth': observer.get('pid_ns_init_birth'),
            'driver': sample['driver'], 'target': sample['target'], 'domain': sample['domain']}


def target_key(sample):
    rows = [item for item in sample['domain'] if item['physical'] == sample['target']['physical']]
    if len(rows) != 1:
        raise ValueError('Requested physical target is ambiguous')
    row = rows[0]
    return sample['observer']['birth']['boot_id'] + ':ascend:' + row['pci'] + ':' + str(row['physical'])


def holder_is_on_target(sample, pid, isolation):
    mapping = isolation.parse_mapping(sample['mapping']['stdout'])
    inventory = isolation.parse_inventory(sample['inventory']['stdout'], mapping)
    target = sample['target']
    return [item for item in inventory['processes'] if item['reported_pid'] == pid] == [
        {'board': target['board'], 'chip': target['chip'], 'reported_pid': pid}]


def sample(folder, label, request, policy, isolation):
    value = isolation.observe_target(policy)
    save(folder / (label + '.json'), value)
    pids = raw_target(value, request, isolation)
    return value, pids


def validate_probe_evidence(request_raw, artifacts):
    """Validate original bytes and return an admitted-policy candidate, CPU only.

    Load this function from the original local receiver snapshot next to its
    pinned tools/ directory. It never imports torch, observes /proc, connects to
    a target, acquires a lease or starts/stops a process. A terminal status word
    alone cannot qualify a probe.
    """
    try:
        return _validate_probe_evidence(request_raw, artifacts)
    except (KeyError, TypeError, IndexError, OSError) as error:
        raise ValueError('Incomplete or changed original PID-view evidence') from error


def _validate_probe_evidence(request_raw, artifacts):
    request = _request(request_raw)
    if not isinstance(artifacts, dict):
        raise ValueError('Original probe artifacts must be a byte mapping')
    required = {*PAYLOAD_FILES, 'npu_pid_view.py', 'request.json', 'probe-result.json', *SUCCESS_RECEIPTS}
    if not required <= set(artifacts):
        raise ValueError('Original successful probe packet is incomplete')
    for name, raw in artifacts.items():
        path = Path(name)
        if (not isinstance(name, str) or path.is_absolute() or '..' in path.parts
                or not isinstance(raw, bytes) or len(raw) > MAX_ARTIFACT_BYTES):
            raise ValueError('Invalid original artifact name or bytes')
    if artifacts['request.json'] != request_raw:
        raise ValueError('Original request bytes changed')
    for name, expected in request['required_files'].items():
        if sha(artifacts[name]) != expected:
            raise ValueError('Original payload hash changed: ' + name)
    if sha(artifacts['npu_pid_view.py']) != request['receiver_sha256']:
        raise ValueError('Original receiver bytes changed')
    processes, isolation, policy = _helpers(request)
    request_sha = sha(request_raw)
    records = {name: json.loads(artifacts[name]) for name in required if name.endswith('.json')}
    if any(not isinstance(value, dict) for value in records.values()):
        raise ValueError('Original JSON receipts must be objects')
    result = records['probe-result.json']
    if (result.get('schema_version') != RESULT_SCHEMA
            or result.get('outcome') != 'candidate_view_evidence_requires_root_admission'
            or result.get('request_sha256') != request_sha or result.get('error') is not None
            or result.get('holder_exit_observed') is not True
            or result.get('performance_claim') is not None or result.get('global_pid_offset_claim') is not None):
        raise ValueError('Original probe did not finish with qualified evidence')
    index = result.get('original_artifacts')
    if not isinstance(index, list) or sha(canonical(index)) != result.get('artifact_index_sha256'):
        raise ValueError('Original artifact index changed')
    names = [item.get('name') for item in index if isinstance(item, dict)]
    # The staging parent may publish its provisional ACK after the detached
    # controller has finished. That ACK is retained but is not exit authority.
    expected_index = {*PAYLOAD_FILES, 'npu_pid_view.py',
                      *(name for name in artifacts if name.endswith('.json') and name not in NONAUTHORITATIVE_JSON)}
    if len(names) != len(index) or len(set(names)) != len(names) or set(names) != expected_index:
        raise ValueError('Original artifact index is incomplete or ambiguous')
    for item in index:
        raw = artifacts[item['name']]
        if type(item.get('bytes')) is not int or item['bytes'] != len(raw) or item.get('sha256') != sha(raw):
            raise ValueError('Original indexed artifact bytes changed')

    def same_identity(left, right):
        return (processes.valid_identity(left) and processes.valid_identity(right)
                and processes.identity_key(left) == processes.identity_key(right)
                and left['uid'] == right['uid']
                and left.get('pid_ns_init_birth') == right.get('pid_ns_init_birth'))

    controller_record = records['controller-start.json']
    observer = controller_record['identity']
    if not processes.valid_identity(observer) or observer['birth']['platform'] != 'linux':
        raise ValueError('Missing exact original Linux observer identity')
    if not same_identity(result['controller'], observer):
        raise ValueError('Original controller identity changed')
    if controller_record.get('profile_sha256') != request['required_files']['profile.json']:
        raise ValueError('Original environment profile binding changed')
    visibility = controller_record.get('visibility')
    if (not isinstance(visibility, dict)
            or set(visibility) != {'ASCEND_VISIBLE_DEVICES', 'ASCEND_RT_VISIBLE_DEVICES'}
            or any(value not in (None, '') for value in visibility.values())):
        raise ValueError('Unqualified device visibility mapping')
    bound_receipts = ('controller-start.json', 'lease-admission.json', 'holder-spawn.json',
                      'holder-start.json', 'holder-ready.json', 'stop-holder.json',
                      'holder-returning.json', 'holder-exit.json')
    for name in bound_receipts:
        if records[name].get('request_sha256') != request_sha:
            raise ValueError('Original receipt/request binding changed: ' + name)
    started = records['holder-start.json']; ready = records['holder-ready.json']
    spawned = records['holder-spawn.json']; exited = records['holder-exit.json']
    returning = records['holder-returning.json']; process = started['identity']
    if not processes.valid_identity(process) or process['ppid'] != observer['pid']:
        raise ValueError('Original holder is not the recorded controller child')
    for identity in (spawned['identity'], ready['identity'], returning['identity'], exited['identity']):
        if not same_identity(identity, process) or identity['ppid'] != observer['pid']:
            raise ValueError('Original holder birth, namespace or parent changed')
    if spawned.get('pid') != process['pid'] or not same_identity(spawned['controller'], observer):
        raise ValueError('Original child spawn binding changed')
    if (not same_identity(exited['controller'], observer) or type(exited.get('returncode')) is not int
            or exited['returncode'] != 0 or exited.get('signal') is not None
            or exited.get('authority') != 'original controller Popen.wait'):
        raise ValueError('No original normal OS exit receipt')
    if (ready.get('logical_device') != policy['logical_device'] or ready.get('allocated_elements') != 1
            or ready.get('dtype') != 'torch.uint8' or ready.get('visibility') != visibility
            or started.get('visibility') != visibility):
        raise ValueError('Holder did not retain the explicit minimal-device contract')

    snapshots = {phase: records[phase + '.json'] for phase in ('before-lease', 'before', 'active', 'after')}
    before = snapshots['before']; active = snapshots['active']; after = snapshots['after']
    pids = {}
    last_end = None
    for phase, value in snapshots.items():
        pids[phase] = raw_target(value, request, isolation)
        if scope(value) != scope(before) or not same_identity(value['observer'], observer):
            raise ValueError('Original boot/namespace/driver/physical scope changed')
        for kind, argv in (('mapping', ['info', '-m']), ('inventory', ['info'])):
            if value[kind].get('argv') != [value['driver']['path'], *argv]:
                raise ValueError('Original device observation command differs')
        start, end = int(value['started_mono_ns']), int(value['ended_mono_ns'])
        if start < 0 or end < start or (last_end is not None and start < last_end):
            raise ValueError('Original observation ordering changed')
        last_end = end
    if any(pids[phase] for phase in ('before-lease', 'before', 'after')) or pids['active'] != [process['pid']]:
        raise ValueError('Original domain does not show empty/sole-holder/empty occupancy')
    if not holder_is_on_target(active, process['pid'], isolation):
        raise ValueError('Original holder appeared on a different physical target')
    admission = records['lease-admission.json']; keys = isolation.device_keys(before)
    leases = admission.get('leases')
    if (admission.get('declared_lease_root') != policy['lease_root'] or admission.get('device_keys') != keys
            or not isinstance(leases, list) or len(leases) != len(keys)
            or [item.get('device_key') for item in leases] != keys
            or started.get('lease_evidence') != leases):
        raise ValueError('Original cooperative lease/domain binding changed')
    for item in leases:
        shared = Path(item['shared_root'])
        if (item.get('schema') != 'kernelbridge.device-lease/v1' or not shared.is_absolute()
                or shared.name != Path(policy['lease_root']).name
                or item.get('root') != str(shared / str(observer['uid']))
                or item.get('filename') != sha(item['device_key'].encode()) + '.lock'
                or item.get('uid') != observer['uid'] or item.get('mnt_ns') != observer['mnt_ns']
                or item.get('boot_id') != observer['birth']['boot_id'] or item.get('platform') != 'linux'
                or any(type(item.get(key)) is not int or item[key] < 0 for key in ('dev', 'ino', 'root_dev', 'root_ino'))):
            raise ValueError('Original cooperative lease scope is incomplete')
    view = records['view-evidence-candidate.json']
    raw_snapshots = {phase: {key: snapshots[phase][key] for key in ('mapping', 'inventory')}
                     for phase in ('before', 'active', 'after')}
    probe = view.get('probe', {})
    if (view.get('raw_snapshots') != raw_snapshots or view.get('observer') != observer
            or view.get('driver') != before['driver'] or view.get('device_key') != target_key(before)
            or not same_identity(probe.get('process'), process)
            or not same_identity(probe.get('active_process'), process)
            or probe.get('reported_pid') != process['pid'] or probe.get('exit_code') != exited['returncode']
            or any(probe.get(phase + '_pids') != pids[phase] for phase in ('before', 'active', 'after'))
            or probe.get('evidence_sha256') != sha(canonical(raw_snapshots))):
        raise ValueError('Process-view projection differs from original raw receipts')
    qualified = isolation.normalize_policy({**policy, 'view_evidence': view})
    processes.validate_view_evidence(view, observer_context=after['observer'], driver_identity=after['driver'])
    isolation._validate_view(qualified, after)
    return qualified


def holder(path, fd_text):
    request, root, folder, request_sha, processes, isolation, visibility = load_request(path)
    lease_record = read(folder / 'lease-admission.json')
    policy = isolation.normalize_policy(request['isolation_policy'])
    fds = [int(part) for part in fd_text.split(',') if part]
    if len(fds) != len(lease_record['leases']) or not fds:
        raise ValueError('Missing inherited cooperative lease')
    for fd, evidence in zip(fds, lease_record['leases']):
        processes.validate_lease(fd, evidence, lease_root=policy['lease_root'], device_key=evidence['device_key'])
    identity = processes.identify_process(os.getpid())
    save(folder / 'holder-start.json', {'identity': identity, 'request_sha256': request_sha,
        'started_ns': str(time.time_ns()), 'visibility': visibility,
        'lease_evidence': lease_record['leases']})
    # These are the only device operations. empty does not initialize data or
    # launch an algorithm; no synchronize, timing, oracle or eager loop exists.
    import torch
    import torch_npu
    device = torch.device('npu', policy['logical_device'])
    torch_npu.npu.set_device(device)
    allocation = torch.empty((1,), dtype=torch.uint8, device=device)
    active_identity = processes.identify_process(os.getpid())
    save(folder / 'holder-ready.json', {'identity': active_identity, 'request_sha256': request_sha,
        'ready_ns': str(time.time_ns()), 'logical_device': int(allocation.device.index),
        'allocated_elements': int(allocation.numel()), 'dtype': str(allocation.dtype),
        'torch_version': str(torch.__version__), 'torch_npu_version': str(torch_npu.__version__),
        'visibility': visibility})
    deadline = time.monotonic() + request['hold_seconds']
    while time.monotonic() < deadline and not (folder / 'stop-holder.json').exists():
        time.sleep(0.1)
    del allocation
    save(folder / 'holder-returning.json', {'request_sha256': request_sha,
        'identity': processes.identify_process(os.getpid()), 'reason': 'normal bounded context release',
        'returning_ns': str(time.time_ns()), 'boundary': 'Not an OS exit receipt; controller wait owns exit authority'})
    # Process teardown closes inherited lease FDs; no explicit LOCK_UN.


def controller(path):
    request, root, folder, request_sha, processes, isolation, visibility = load_request(path)
    observer = processes.identify_process(os.getpid())
    save(folder / 'controller-start.json', {'identity': observer, 'request_sha256': request_sha,
        'started_ns': str(time.time_ns()), 'visibility': visibility,
        'profile_sha256': request['required_files']['profile.json'],
        'device_nodes': {str(p): {'major': os.major(p.stat().st_rdev), 'minor': os.minor(p.stat().st_rdev)}
                         for p in Path('/dev').glob('davinci*') if p.exists()}})
    policy = isolation.normalize_policy(request['isolation_policy'])
    leases = []; child = None; exit_record = None; outcome = 'not_started'; error = None
    try:
        initial, initial_pids = sample(folder, 'before-lease', request, policy, isolation)
        if initial_pids: raise ValueError('Target chip is occupied before cooperative lease')
        keys = isolation.device_keys(initial)
        if not keys: raise ValueError('The requested physical isolation domain is empty')
        for key in keys: leases.append(processes.acquire_lease(key, policy['lease_root']))
        before, before_pids = sample(folder, 'before', request, policy, isolation)
        if before_pids or scope(before) != scope(initial) or isolation.device_keys(before) != keys:
            raise ValueError('Target chip occupancy or scope changed during lease acquisition')
        save(folder / 'lease-admission.json', {'request_sha256': request_sha,
            'declared_lease_root': policy['lease_root'],
            'leases': [lease.evidence for lease in leases], 'device_keys': keys,
            'scope': 'same UID and shared lease mount only; external noncooperators are not excluded'})
        log = (folder / 'holder.log').open('xb')
        child = subprocess.Popen([sys.executable, '-B', str(Path(__file__)), 'holder', str(path),
            ','.join(str(lease.fd) for lease in leases)], stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, pass_fds=tuple(lease.fd for lease in leases))
        log.close()
        save(folder / 'holder-spawn.json', {'pid': child.pid, 'identity': processes.identify_process(child.pid),
            'controller': observer, 'request_sha256': request_sha, 'spawned_ns': str(time.time_ns())})
        ready_deadline = time.monotonic() + 60
        while not (folder / 'holder-ready.json').exists() and child.poll() is None and time.monotonic() < ready_deadline:
            time.sleep(0.1)
        if not (folder / 'holder-ready.json').exists():
            raise ValueError('Original holder did not become ready; no replacement is permitted')
        started = read(folder / 'holder-start.json'); ready = read(folder / 'holder-ready.json')
        if (started['request_sha256'] != request_sha or ready['request_sha256'] != request_sha
                or processes.identity_key(started['identity']) != processes.identity_key(ready['identity'])
                or ready['logical_device'] != policy['logical_device'] or ready['allocated_elements'] != 1):
            raise ValueError('Holder runtime or exact process identity changed')
        active_identity = processes.identify_process(child.pid)
        if processes.identity_key(active_identity) != processes.identity_key(started['identity']):
            raise ValueError('Original holder birth or namespace changed')
        active, active_pids = sample(folder, 'active', request, policy, isolation)
        if (active_pids != [child.pid] or scope(active) != scope(before)
                or not holder_is_on_target(active, child.pid, isolation)):
            raise ValueError('Driver PID does not exactly match the sole holder in the reviewed scope')
        save(folder / 'stop-holder.json', {'request_sha256': request_sha, 'requested_ns': str(time.time_ns()),
            'purpose': 'normal exit of this probe child only; not a remote process signal'})
        returncode = child.wait(timeout=30)
        exit_record = {'request_sha256': request_sha, 'identity': started['identity'],
            'controller': observer, 'returncode': returncode, 'signal': -returncode if returncode < 0 else None,
            'observed_exit_ns': str(time.time_ns()), 'authority': 'original controller Popen.wait'}
        save(folder / 'holder-exit.json', exit_record)
        if returncode != 0: raise ValueError('Original holder did not exit normally')
        after, after_pids = sample(folder, 'after', request, policy, isolation)
        if after_pids or scope(after) != scope(before):
            raise ValueError('Post-exit target chip or driver/namespace scope is not clear')
        raw_snapshots = {name: {key: value[key] for key in ('mapping', 'inventory')}
                         for name, value in (('before', before), ('active', active), ('after', after))}
        view = {'schema': 'kernelbridge.process-view/v1', 'kind': 'verified_local_pidns',
            'observer': observer, 'driver': before['driver'], 'device_key': target_key(before),
            'probe': {'process': started['identity'], 'active_process': active_identity,
                'reported_pid': child.pid, 'before_pids': before_pids, 'active_pids': active_pids,
                'after_pids': after_pids, 'exit_code': returncode, 'evidence_sha256': sha(canonical(raw_snapshots))},
            'raw_snapshots': raw_snapshots}
        processes.validate_view_evidence(view, observer_context=after['observer'], driver_identity=after['driver'])
        isolation._validate_view({**policy, 'view_evidence': view}, after)
        save(folder / 'view-evidence-candidate.json', view)
        outcome = 'candidate_view_evidence_requires_root_admission'
    except Exception as caught:
        error = {'type': type(caught).__name__, 'message': str(caught)[:2048]}
        outcome = 'probe_inconclusive' if child is not None else 'not_started'
    finally:
        if child is not None and exit_record is None:
            if not (folder / 'stop-holder.json').exists():
                save(folder / 'stop-holder.json', {'request_sha256': request_sha,
                    'requested_ns': str(time.time_ns()), 'purpose': 'normal cleanup of this probe child only'})
            try:
                returncode = child.wait(timeout=30)
                start = read(folder / 'holder-start.json') if (folder / 'holder-start.json').exists() else None
                exit_record = {'request_sha256': request_sha, 'identity': start['identity'] if start else None,
                    'spawned_pid': child.pid, 'controller': observer, 'returncode': returncode,
                    'signal': -returncode if returncode < 0 else None, 'observed_exit_ns': str(time.time_ns()),
                    'authority': 'original controller Popen.wait', 'qualification': 'inconclusive'}
                save(folder / 'holder-exit.json', exit_record)
            except subprocess.TimeoutExpired:
                outcome = 'original_holder_exit_unknown'
                # Never kill another PID or release the child's inherited lease.
                # Child retains its inherited open FD until its actual exit.
        for lease in leases: lease.close()
        records = []
        indexed = {*PAYLOAD_FILES, 'npu_pid_view.py',
                   *(item.name for item in folder.glob('*.json') if item.name not in NONAUTHORITATIVE_JSON)}
        for name in sorted(indexed):
            raw = (folder / name).read_bytes(); records.append({'name': name, 'bytes': len(raw), 'sha256': sha(raw)})
        save(folder / 'probe-result.json', {'schema_version': RESULT_SCHEMA,
            'request_sha256': request_sha, 'controller': observer, 'outcome': outcome, 'error': error,
            'holder_exit_observed': exit_record is not None, 'original_artifacts': records,
            'artifact_index_sha256': sha(canonical(records)), 'performance_claim': None,
            'global_pid_offset_claim': None, 'root_must_explicitly_admit_view': True,
            'unknown_rule': 'Recover only this original probe; never redispatch or replace after uncertain ACK'})
    return 0 if outcome == 'candidate_view_evidence_requires_root_admission' else 2


if __name__ == '__main__':
    action, path = sys.argv[1], Path(sys.argv[2]).absolute()
    if action == 'controller': raise SystemExit(controller(path))
    if action == 'holder': holder(path, sys.argv[3])
    else: raise SystemExit('Only original controller or holder entry is supported')
