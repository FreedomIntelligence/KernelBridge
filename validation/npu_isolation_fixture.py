"""Synthetic original bytes for the real isolation parser and validators."""
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/scatter'))
import npu_isolation as isolation


def identity(pid, parent=1, tick=None):
    return {'pid': pid, 'ppid': parent, 'uid': 1000, 'state': 'S', 'pid_ns': 'pid:[123]', 'mnt_ns': 'mnt:[456]',
            'nspid': [pid], 'birth': {'platform': 'linux', 'boot_id': '00000000-0000-0000-0000-000000000001',
                                    'start_ticks': str(tick or pid)}}


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


def seal(fixture):
    """Rebind synthetic evidence after a deliberate fixture mutation."""
    fixture['request_raw'] = isolation.canonical(fixture['request']) + b'\n'
    request_sha = isolation.digest(fixture['request_raw'])
    fixture['handle']['execution_request_sha256'] = request_sha
    fixture['exit']['handle'] = copy.deepcopy(fixture['handle'])
    fixture['exit']['execution_request_sha256'] = request_sha
    fixture['exit_raw'] = isolation.canonical(fixture['exit']) + b'\n'
    fixture['samples_raw'] = b''.join(isolation.canonical(row) + b'\n' for row in fixture['samples'])
    fixture['receipt'].update(handle=copy.deepcopy(fixture['handle']), execution_request_sha256=request_sha,
        execution_exit_sha256=isolation.digest(fixture['exit_raw']), policy_sha256=isolation.policy_digest(fixture['policy']),
        samples_sha256=isolation.digest(fixture['samples_raw']))
    fixture['receipt_raw'] = isolation.canonical(fixture['receipt']) + b'\n'
    fixture['raw'] = {name: fixture[key] for name, key in (
        ('execution-request.json', 'request_raw'), ('execution-exit.json', 'exit_raw'),
        ('isolation-samples.jsonl', 'samples_raw'), ('isolation-receipt.json', 'receipt_raw'))}
    return fixture


def build_fixture(source_sha256='a' * 64, result_sha256='b' * 64, action_id='original-E', logical_device=0):
    if type(logical_device) is not int or logical_device not in (0, 1):
        raise ValueError('Synthetic fixture supports only logical device 0 or 1')
    observer, child, probe = identity(100), identity(200, 100), identity(400)
    driver = {'path': '/usr/bin/npu-smi', 'sha256': 'd' * 64, 'version_text': '25.5.2'}
    pci = ('0000:9d:00.0', '0000:9f:00.0')[logical_device]
    key = observer['birth']['boot_id'] + ':ascend:' + pci + ':' + str(logical_device)
    raw_snapshots = {name: snapshot(rows) for name, rows in (('before', []), ('active', [(logical_device, 400)]), ('after', []))}
    view = {'schema': 'kernelbridge.process-view/v1', 'kind': 'verified_local_pidns', 'observer': observer, 'driver': driver,
            'device_key': key, 'raw_snapshots': raw_snapshots,
            'probe': {'process': probe, 'active_process': probe, 'reported_pid': 400,
                      'before_pids': [], 'active_pids': [400], 'after_pids': [], 'exit_code': 0,
                      'evidence_sha256': isolation.digest(isolation.canonical(raw_snapshots))}}
    policy = isolation.normalize_policy({'logical_device': logical_device, 'view_evidence': view})
    code_paths = {'supervisor_sha256': ROOT / 'examples/scatter/npu_job_supervisor.py',
                  'isolation_code_sha256': ROOT / 'examples/scatter/npu_isolation.py',
                  'process_isolation_code_sha256': ROOT / 'examples/operator_pipeline/process_isolation.py',
                  'npu_enter_code_sha256': ROOT / 'examples/scatter/npu_enter.py'}
    pins = {name: isolation.digest(path.read_bytes()) for name, path in code_paths.items()}
    intent = {'action_id': action_id, 'attempt': 1, 'source_sha256': source_sha256, 'mode': 'fixture', 'device': logical_device}
    remote = '/tmp/native-isolation-fixture'; attempt = remote + '/attempts/one'
    request = {'schema_version': 'kernelbridge.native-execution-request/v1', 'intent': intent, 'argv': ['/bin/true'],
               'remote_root': remote, 'canonical_root': remote, 'remote_attempt': attempt, 'isolation_policy': policy,
               'claim_lock': {'kind': 'original_attempt', 'path': remote + '/.npu-claim-' + isolation.digest(attempt.encode()) + '.lock'}, **pins}
    owner = {'pid': observer['pid'], 'birth': observer['birth'], 'code_sha256': pins['supervisor_sha256']}
    start = 1794490000000000000
    handle = {'schema_version': 'kernelbridge.native-execution-handle/v1', 'pid': child['pid'], 'process_birth': child['birth'],
              'supervisor': owner, 'started_ns': str(start), 'attempt': attempt, 'argv': request['argv'],
              'remote_root': remote, 'canonical_root': remote, 'remote_attempt': attempt}
    exit_record = {'schema_version': 'kernelbridge.native-execution-exit/v1', 'intent': intent, 'supervisor': owner,
                   'process_birth': child['birth'], 'returncode': 0, 'signal': None, 'finished_ns': str(start + 2000000000),
                   'remote_root': remote, 'canonical_root': remote, 'remote_attempt': attempt}
    lease = {'schema': 'kernelbridge.device-lease/v1', 'device_key': key, 'shared_root': policy['lease_root'],
             'root': policy['lease_root'] + '/1000', 'filename': isolation.digest(key.encode()) + '.lock',
             'root_dev': 1, 'root_ino': 2, 'dev': 1, 'ino': 3, 'uid': 1000, 'mnt_ns': observer['mnt_ns'],
             'boot_id': observer['birth']['boot_id'], 'platform': 'linux'}
    samples = []
    for index, (phase, delta) in enumerate((('admission', -3000000000), ('admission_locked', -2000000000),
                                           ('running', 100000000), ('final', 2100000000))):
        sample = snapshot([(logical_device, child['pid'])] if phase == 'running' else [])
        target, domain, rows = isolation._parsed(sample, policy)
        sample.update(schema_version=isolation.SAMPLE_SCHEMA, observer=observer, driver=driver,
                      target=target, domain=domain, processes=[], sequence=index, phase=phase,
                      observed_ns=str(start + delta), ended_ns=str(start + delta + 10000000),
                      started_mono_ns=str(10000000000 + delta), ended_mono_ns=str(10010000000 + delta),
                      visibility={'ASCEND_VISIBLE_DEVICES': '', 'ASCEND_RT_VISIBLE_DEVICES': ''},
                      lease_evidence=[] if index == 0 else [lease])
        if rows: sample['processes'] = [{'reported': rows[0], 'identity': child, 'lineage': [child], 'lineage_source': 'fresh'}]
        samples.append(sample)
    receipt = {'schema_version': isolation.RECEIPT_SCHEMA, 'intent': intent,
               **{k: v for k, v in pins.items() if k != 'supervisor_sha256'},
               'own_root': child, 'leases': [lease], 'result_sha256': result_sha256, 'observer_error': None}
    return seal({'policy': policy, 'intent': intent, 'handle': handle, 'request': request, 'exit': exit_record,
                 'samples': samples, 'receipt': receipt, 'source_sha256': source_sha256, 'result_sha256': result_sha256,
                 'code_paths': code_paths})


def preparation_profile(device=0):
    policy = build_fixture(logical_device=device)['policy']
    return {'validation_fixture': {'kind': 'synthetic_pid_view', 'device_evidence': False},
            'device_isolation': {'policy': policy, 'policy_sha256': isolation.policy_digest(policy)}}


def build_admission_fixture(reason_code='observation_unknown', **kwargs):
    fixture = build_fixture(**kwargs)
    fixture['samples'] = fixture['samples'][:1]
    sample = fixture['samples'][0]
    if reason_code == 'observation_unknown':
        sample['error'] = 'Original driver observation was unavailable'
    elif reason_code == 'foreign':
        sample.update(snapshot([(fixture['policy']['logical_device'], 600)]))
        _, _, rows = isolation._parsed(sample, fixture['policy'])
        sample['processes'] = [{'reported': rows[0], 'identity': identity(600), 'lineage': None, 'lineage_source': 'fresh'}]
    elif reason_code != 'cooperative_lease_busy':
        raise ValueError('Unsupported fixture admission reason')
    seal(fixture)
    _, binding = isolation._bindings(fixture['request_raw'], fixture['intent'], fixture['policy'])
    record = {'schema_version': isolation.ADMISSION_SCHEMA, 'status': 'not_started', 'dispatched': False,
              **binding, 'reason': 'Synthetic original admission: ' + reason_code, 'reason_code': reason_code,
              'samples_sha256': isolation.digest(fixture['samples_raw']), 'supervisor': fixture['handle']['supervisor']}
    if reason_code == 'cooperative_lease_busy':
        record.update(busy_device_key=isolation.device_keys(sample)[0], lease_root=fixture['policy']['lease_root'], lease_scope=isolation.SCOPE)
    fixture['admission'] = record
    fixture['admission_raw'] = isolation.canonical(record) + b'\n'
    return fixture
