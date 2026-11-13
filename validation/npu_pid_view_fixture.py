"""Synthetic complete PID-view packets for the real CPU-only validator.

Only local temporary snapshots are written. No probe/controller/holder runs.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def identity(pid, parent=1):
    birth = {'platform': 'linux', 'boot_id': '00000000-0000-0000-0000-000000000001', 'start_ticks': str(pid)}
    return {'pid': pid, 'ppid': parent, 'uid': 1000, 'state': 'S', 'pid_ns': 'pid:[123]',
            'mnt_ns': 'mnt:[456]', 'nspid': [pid], 'birth': birth,
            'pid_ns_init_birth': {**birth, 'start_ticks': '1'}}


def snapshot(rows=()):
    mapping = 'NPU ID Chip ID Chip Logic ID Chip Phy-ID Chip Name\n0 0 0 0 Ascend910\n0 1 1 1 Ascend910\n'
    inventory = ('| npu-smi 25.5.2 Version: 25.5.2 |\n| NPU Name | Health | Power Temp |\n'
                 '| 0 Ascend910 | OK | 100 40 |\n| 0 0 | 0000:9d:00.0 | 0 0/0 100/65536 |\n'
                 '| 0 Ascend910 | OK | 100 40 |\n| 1 1 | 0000:9f:00.0 | 0 0/0 100/65536 |\n'
                 '+----------------+\n| NPU Chip | Process id | Process name | Process memory(MB) |\n+================+\n')
    inventory += ''.join(f'| 0 {chip} | {pid} | python | 100 |\n' for chip, pid in rows) if rows else '| No running processes found in NPU 0 |\n'
    inventory += '+================+\n'
    return {'mapping': {'returncode': 0, 'stdout': mapping, 'stderr': '', 'argv': ['/usr/bin/npu-smi', 'info', '-m']},
            'inventory': {'returncode': 0, 'stdout': inventory, 'stderr': '', 'argv': ['/usr/bin/npu-smi', 'info']}}


def load_snapshot(path):
    raw = path.read_bytes(); name = 'pid_view_fixture_' + digest(raw) + '_' + digest(str(path).encode())[:12]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


def seal(packet):
    """Re-index a deliberate synthetic mutation without changing its semantics."""
    for name, value in packet['records'].items():
        if name != 'probe-result.json': packet['artifacts'][name] = canonical(value) + b'\n'
    packet['artifacts']['request.json'] = packet['request_raw']
    receiver = packet['receiver']
    names = {*receiver.PAYLOAD_FILES, 'npu_pid_view.py',
             *(name for name in packet['artifacts'] if name.endswith('.json') and name not in receiver.NONAUTHORITATIVE_JSON)}
    index = [{'name': name, 'bytes': len(packet['artifacts'][name]), 'sha256': digest(packet['artifacts'][name])}
             for name in sorted(names)]
    result = packet['records']['probe-result.json']
    result.update(original_artifacts=index, artifact_index_sha256=digest(canonical(index)))
    packet['artifacts']['probe-result.json'] = canonical(result) + b'\n'
    return packet


def build_packet(folder, *, logical_device=0, domain_physical_ids=None, receiver_path=None,
                 request=None, request_raw=None):
    folder = Path(folder).resolve(); folder.mkdir(parents=True, exist_ok=True); (folder / 'tools').mkdir(exist_ok=True)
    sources = {'tools/npu_enter.py': ROOT / 'examples/scatter/npu_enter.py',
               'tools/npu_isolation.py': ROOT / 'examples/scatter/npu_isolation.py',
               'tools/process_isolation.py': ROOT / 'examples/operator_pipeline/process_isolation.py',
               'npu_pid_view.py': Path(receiver_path) if receiver_path else ROOT / 'examples/scatter/npu_pid_view.py'}
    for name, source in sources.items():
        path = folder / name; raw = source.read_bytes()
        if path.exists():
            if request is None and path.read_bytes() != raw: raise ValueError('Different fixture source already exists')
        else: path.write_bytes(raw)
    profile = folder / 'profile.json'
    if not profile.exists(): profile.write_bytes(canonical({'sdk': '/fixture/sdk', 'python': '/fixture/python'}) + b'\n')
    payload = {name: (folder / name).read_bytes() for name in (*sources, 'profile.json')}
    receiver = load_snapshot(folder / 'npu_pid_view.py')
    if request is None:
        # Load the frozen helper only to normalize/parse CPU fixture data.
        isolation = load_snapshot(folder / 'tools/npu_isolation.py')
        declared = {'logical_device': logical_device, 'lease_root': '/fixture/shared-leases'}
        if domain_physical_ids is not None: declared['domain_physical_ids'] = domain_physical_ids
        policy = isolation.normalize_policy(declared)
        root = '/fixture/authorized-root'; probe_id = 'pid-view-' + '1' * 32
        request = {'schema_version': receiver.REQUEST_SCHEMA, 'probe_id': probe_id,
                   'remote_root': root, 'remote_probe': root + '/.infrastructure-probes/' + probe_id,
                   'isolation_policy': policy, 'hold_seconds': 12,
                   'allocation': {'elements': 1, 'dtype': 'uint8', 'algorithm': None},
                   'required_files': {name: digest(payload[name]) for name in receiver.PAYLOAD_FILES},
                   'receiver_sha256': digest(payload['npu_pid_view.py'])}
    request = copy.deepcopy(request)
    raw = request_raw if request_raw is not None else canonical(request) + b'\n'
    if json.loads(raw) != request: raise ValueError('Original fixture request bytes differ')
    processes, isolation, policy = receiver._helpers(request)
    for name, expected in request['required_files'].items():
        if digest(payload[name]) != expected: raise ValueError('Fixture payload differs from prepared request')
    request_sha = digest(raw); observer = identity(100); child = identity(200, 100)
    visibility = {'ASCEND_VISIBLE_DEVICES': None, 'ASCEND_RT_VISIBLE_DEVICES': None}
    driver = {'path': '/usr/bin/npu-smi', 'sha256': 'd' * 64, 'version_text': '25.5.2'}
    records = {}; samples = {}
    for i, phase in enumerate(('before-lease', 'before', 'active', 'after')):
        value = snapshot([(policy['logical_device'], child['pid'])] if phase == 'active' else [])
        target, domain, _ = isolation._parsed(value, policy)
        value.update(schema_version=isolation.SAMPLE_SCHEMA, observer=copy.deepcopy(observer), driver=copy.deepcopy(driver),
                     target=target, domain=domain, processes=[], error='ValueError: No qualified driver PID-view proof',
                     visibility=copy.deepcopy(visibility), observed_ns=str(1794500000000000000 + i * 1000),
                     started_mono_ns=str((i + 1) * 1000000), ended_mono_ns=str((i + 1) * 1000000 + 1000))
        records[phase + '.json'] = value; samples[phase] = value
    keys = isolation.device_keys(samples['before'])
    leases = [{'schema': 'kernelbridge.device-lease/v1', 'device_key': key,
               'shared_root': policy['lease_root'], 'root': policy['lease_root'] + '/1000',
               'filename': digest(key.encode()) + '.lock', 'root_dev': 1, 'root_ino': 2,
               'dev': 1, 'ino': i + 10, 'uid': 1000, 'mnt_ns': observer['mnt_ns'],
               'boot_id': observer['birth']['boot_id'], 'platform': 'linux'} for i, key in enumerate(keys)]
    records.update({
        'controller-start.json': {'identity': copy.deepcopy(observer), 'request_sha256': request_sha,
            'started_ns': '1794500000000000000', 'visibility': copy.deepcopy(visibility),
            'profile_sha256': request['required_files']['profile.json'], 'device_nodes': {}},
        'lease-admission.json': {'request_sha256': request_sha, 'declared_lease_root': policy['lease_root'],
            'leases': copy.deepcopy(leases), 'device_keys': keys},
        'holder-spawn.json': {'pid': child['pid'], 'identity': copy.deepcopy(child), 'controller': copy.deepcopy(observer),
            'request_sha256': request_sha, 'spawned_ns': '1794500000000001000'},
        'holder-start.json': {'identity': copy.deepcopy(child), 'request_sha256': request_sha,
            'started_ns': '1794500000000001100', 'visibility': copy.deepcopy(visibility), 'lease_evidence': copy.deepcopy(leases)},
        'holder-ready.json': {'identity': copy.deepcopy(child), 'request_sha256': request_sha,
            'ready_ns': '1794500000000001200', 'logical_device': policy['logical_device'], 'allocated_elements': 1,
            'dtype': 'torch.uint8', 'torch_version': 'fixture', 'torch_npu_version': 'fixture', 'visibility': copy.deepcopy(visibility)},
        'stop-holder.json': {'request_sha256': request_sha, 'requested_ns': '1794500000000002000', 'purpose': 'normal exit'},
        'holder-returning.json': {'request_sha256': request_sha, 'identity': copy.deepcopy(child),
            'reason': 'normal bounded context release', 'returning_ns': '1794500000000002100'},
        'holder-exit.json': {'request_sha256': request_sha, 'identity': copy.deepcopy(child), 'controller': copy.deepcopy(observer),
            'returncode': 0, 'signal': None, 'observed_exit_ns': '1794500000000002200', 'authority': 'original controller Popen.wait'},
        'dispatch-intent.json': {'request_sha256': request_sha},
    })
    snapshots = {phase: {name: copy.deepcopy(samples[phase][name]) for name in ('mapping', 'inventory')}
                 for phase in ('before', 'active', 'after')}
    records['view-evidence-candidate.json'] = {'schema': 'kernelbridge.process-view/v1', 'kind': 'verified_local_pidns',
        'observer': copy.deepcopy(observer), 'driver': copy.deepcopy(driver), 'device_key': receiver.target_key(samples['before']),
        'probe': {'process': copy.deepcopy(child), 'active_process': copy.deepcopy(child), 'reported_pid': child['pid'],
            'before_pids': [], 'active_pids': [child['pid']], 'after_pids': [], 'exit_code': 0,
            'evidence_sha256': digest(canonical(snapshots))}, 'raw_snapshots': snapshots}
    records['probe-result.json'] = {'schema_version': receiver.RESULT_SCHEMA, 'request_sha256': request_sha,
        'controller': copy.deepcopy(observer), 'outcome': 'candidate_view_evidence_requires_root_admission', 'error': None,
        'holder_exit_observed': True, 'performance_claim': None, 'global_pid_offset_claim': None,
        'root_must_explicitly_admit_view': True}
    packet = {'folder': folder, 'receiver': receiver, 'request': request, 'request_raw': raw,
              'artifacts': payload, 'records': records, 'policy': policy, 'samples': samples}
    return seal(packet)
