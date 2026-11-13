"""Ascend occupancy observations bound to one native execution.

This proves only sampled observations and cooperative same-UID shared-directory
leases. It does not claim global exclusivity or replace benchmark correctness.
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time

def _module(name, expected=None):
    path = Path(__file__).resolve().with_name(name + '.py')
    if not path.is_file() and name == 'process_isolation':
        path = Path(__file__).resolve().parents[1] / 'operator_pipeline' / (name + '.py')
    if path.resolve() != path: raise ValueError('Linked isolation dependency')
    raw = path.read_bytes(); source_sha = hashlib.sha256(raw).hexdigest()
    if expected is not None and source_sha != expected: raise ValueError('Pinned isolation dependency changed')
    module_name = 'native_isolation_' + name + '_' + source_sha
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    module.__source_sha256__ = source_sha
    return module


processes = _module('process_isolation')
SELF_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

POLICY_SCHEMA = 'kernelbridge.npu-isolation-policy/v1'
SAMPLE_SCHEMA = 'kernelbridge.npu-isolation-sample/v1'
RECEIPT_SCHEMA = 'kernelbridge.npu-isolation-receipt/v1'
ADMISSION_SCHEMA = 'kernelbridge.native-admission/v1'
CLAIM = 'sampled_observed_clear'
SCOPE = 'cooperative_same_uid_namespace'
MAX_BYTES = 16 * 1024 * 1024
MAX_SAMPLE_BYTES = MAX_BYTES // 4


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def normalize_policy(value):
    if not isinstance(value, dict): raise ValueError('Isolation policy must be an object')
    allowed = {'schema_version', 'required', 'logical_device', 'lease_root', 'domain_physical_ids',
               'sample_interval_ms', 'sample_timeout_ms', 'max_gap_ms', 'view_evidence', 'claim', 'scope'}
    if set(value) - allowed: raise ValueError('Unknown isolation policy fields')
    policy = dict(schema_version=POLICY_SCHEMA, required=True, lease_root='/tmp/kernelbridge-npu-leases',
                  sample_interval_ms=1000, sample_timeout_ms=2000, max_gap_ms=5000,
                  claim=CLAIM, scope=SCOPE)
    policy.update(value)
    if (policy['schema_version'] != POLICY_SCHEMA or policy['required'] is not True
            or policy['claim'] != CLAIM or policy['scope'] != SCOPE):
        raise ValueError('Isolation policy cannot weaken its required claim or scope')
    if type(policy.get('logical_device')) is not int or policy['logical_device'] < 0:
        raise ValueError('Isolation requires one logical device')
    root = Path(policy['lease_root'])
    if not root.is_absolute() or '..' in root.parts: raise ValueError('Invalid shared lease root')
    for name, low, high in (('sample_interval_ms', 50, 60000), ('sample_timeout_ms', 50, 10000), ('max_gap_ms', 100, 120000)):
        if type(policy[name]) is not int or not low <= policy[name] <= high:
            raise ValueError('Invalid isolation sampling bound: ' + name)
    if policy['max_gap_ms'] < policy['sample_interval_ms'] + policy['sample_timeout_ms']:
        raise ValueError('Isolation maximum gap is shorter than its bounded observation interval')
    if 'domain_physical_ids' in policy:
        ids = policy['domain_physical_ids']
        if (not isinstance(ids, list) or not ids or len(ids) > 64
                or any(type(v) is not int or v < 0 for v in ids) or len(set(ids)) != len(ids)):
            raise ValueError('Invalid explicitly expanded isolation domain')
        policy['domain_physical_ids'] = sorted(ids)
    if 'view_evidence' in policy and not isinstance(policy['view_evidence'], dict):
        raise ValueError('Invalid process-view evidence')
    return json.loads(canonical(policy))


def policy_digest(policy):
    return digest(canonical(normalize_policy(policy)))


def parse_mapping(text):
    if not isinstance(text, str) or 'Chip Logic ID' not in text or 'Chip Phy-ID' not in text:
        raise ValueError('Unrecognized Ascend logical/physical mapping')
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or not parts[0].isdigit(): continue
        if len(parts) != 5 or not parts[1].isdigit(): raise ValueError('Malformed Ascend mapping row')
        if parts[2:4] == ['-', '-'] and parts[4].lower() == 'mcu': continue
        if not all(p.isdigit() for p in parts[:4]): raise ValueError('Unresolved Ascend mapping row')
        rows.append(dict(zip(('board', 'chip', 'logical', 'physical'), map(int, parts[:4]))))
    if not rows or any(len({row[key] for row in rows}) != len(rows) for key in ('logical', 'physical')):
        raise ValueError('Missing or ambiguous Ascend physical mapping')
    if len({(r['board'], r['chip']) for r in rows}) != len(rows): raise ValueError('Duplicate Ascend chip mapping')
    return rows


def parse_inventory(text, mapping):
    if not isinstance(text, str) or 'npu-smi' not in text or 'Process id' not in text:
        raise ValueError('Unrecognized Ascend process inventory')
    chips, rows, board, health, in_process = [], [], None, None, False
    process_data = False; process_closed = False; covered_boards = set(); empty_all = False
    for line in text.splitlines():
        if 'Process id' in line:
            in_process = True; continue
        if not line.strip().startswith('|'):
            if in_process and line.strip():
                if re.fullmatch(r'\+[+=-]+\+', line.strip()):
                    if process_data: process_closed = True
                else: raise ValueError('Unrecognized content in Ascend process table')
            continue
        cells = [part.strip() for part in line.strip().strip('|').split('|')]
        first = cells[0].split()
        if not first or not first[0].isdigit():
            if in_process:
                empty = re.fullmatch(r'No running processes?(?: found)?(?: in NPU (?:ID )?(\d+))?[.]?', ' '.join(cells).strip(), re.IGNORECASE)
                if empty:
                    process_data = True; process_closed = False
                    if empty.group(1) is None: empty_all = True
                    else: covered_boards.add(int(empty.group(1)))
                    continue
                raise ValueError('Unrecognized Ascend process-table content')
            continue
        if in_process:
            if (len(cells) != 4 or len(first) != 2 or not all(p.isdigit() for p in first)
                    or not cells[1].isdigit()): raise ValueError('Malformed Ascend process row')
            row = {'board': int(first[0]), 'chip': int(first[1]), 'reported_pid': int(cells[1])}
            if row['reported_pid'] <= 0: raise ValueError('Invalid reported device process')
            rows.append(row)
            process_data = True; process_closed = False; covered_boards.add(row['board'])
        elif len(first) == 2 and first[1].isdigit():
            if board is None or len(cells) < 2 or not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', cells[1]):
                raise ValueError('Unresolved Ascend PCI identity')
            if not health or len(cells) < 3 or not cells[2]: raise ValueError('Incomplete Ascend chip health record')
            chips.append({'board': board, 'chip': int(first[0]), 'physical': int(first[1]), 'pci': cells[1].lower(), 'health': health})
        elif len(first) >= 2:
            board = int(first[0])
            health = cells[1] if len(cells) >= 3 and cells[2] else None
    if not process_data or not process_closed: raise ValueError('Incomplete or header-only Ascend process table')
    expected = {(r['board'], r['chip'], r['physical']) for r in mapping}
    if {(r['board'], r['chip'], r['physical']) for r in chips} != expected or len(chips) != len(mapping):
        raise ValueError('Inventory and mapping disagree on physical chips')
    if len({r['pci'] for r in chips}) != len(chips): raise ValueError('Ambiguous Ascend PCI identities')
    if any((r['board'], r['chip']) not in {(m['board'], m['chip']) for m in mapping} for r in rows):
        raise ValueError('Device process belongs to an unmapped chip')
    return {'chips': chips, 'processes': rows,
            'covered_boards': sorted({r['board'] for r in mapping} if empty_all else covered_boards)}


def _parsed(sample, policy):
    for key in ('mapping', 'inventory'):
        if sample[key]['returncode'] != 0: raise ValueError('Ascend observation command did not succeed')
    mapping = parse_mapping(sample['mapping']['stdout'])
    inventory = parse_inventory(sample['inventory']['stdout'], mapping)
    targets = [r for r in mapping if r['logical'] == policy['logical_device']]
    if len(targets) != 1: raise ValueError('Logical device has no unique physical target')
    target = targets[0]
    physical_ids = policy.get('domain_physical_ids', [target['physical']])
    if target['physical'] not in physical_ids: raise ValueError('Expanded domain excludes its requested device')
    domain = [r for r in inventory['chips'] if r['physical'] in physical_ids]
    if len(domain) != len(physical_ids): raise ValueError('Expanded domain contains an unmapped device')
    if any(r['health'] != 'OK' or r['board'] not in inventory['covered_boards'] for r in domain):
        raise ValueError('Target chip health or complete process inventory is unavailable')
    selected = {(r['board'], r['chip']) for r in domain}
    rows = [r for r in inventory['processes'] if (r['board'], r['chip']) in selected]
    return target, sorted(domain, key=lambda x: (x['board'], x['chip'])), rows


def _boot(identity):
    return identity['birth']['boot_id']


def device_keys(sample):
    return sorted(_boot(sample['observer']) + ':ascend:' + row['pci'] + ':' + str(row['physical']) for row in sample['domain'])


def _validate_view(policy, sample):
    view = policy.get('view_evidence')
    if not isinstance(view, dict): raise ValueError('No qualified driver PID-view proof')
    processes.validate_view_evidence(view, observer_context=sample['observer'], driver_identity=sample['driver'])
    snapshots = view.get('raw_snapshots')
    if not isinstance(snapshots, dict) or set(snapshots) != {'before', 'active', 'after'}:
        raise ValueError('PID-view proof lacks original occupancy snapshots')
    if digest(canonical(snapshots)) != view['probe']['evidence_sha256']:
        raise ValueError('PID-view original snapshots changed')
    for phase, snapshot in snapshots.items():
        target, domain, rows = _parsed(snapshot, policy)
        if target != sample['target'] or domain != sample['domain']:
            raise ValueError('PID-view proof used a different physical target or domain')
        pids = sorted({row['reported_pid'] for row in rows})
        if pids != sorted(view['probe'][phase + '_pids']): raise ValueError('PID-view process list differs from original snapshot')
    target_chips = [row for row in sample['domain'] if row['physical'] == sample['target']['physical']]
    if len(target_chips) != 1: raise ValueError('PID-view proof has no unique physical target')
    target_chip = target_chips[0]
    target_key = _boot(sample['observer']) + ':ascend:' + target_chip['pci'] + ':' + str(target_chip['physical'])
    if view['device_key'] != target_key: raise ValueError('PID-view proof belongs to another physical chip')


def validate_policy_view(policy):
    """Validate retained PID-view proof, without observing current occupancy.

    This checks the profile's internally bound probe bytes only. Launch admission
    must still revalidate the current boot, namespace, driver, and device view.
    """
    normalized = normalize_policy(policy)
    view = normalized.get('view_evidence')
    if not isinstance(view, dict): raise ValueError('No qualified driver PID-view proof')
    try:
        target, domain, _ = _parsed(view['raw_snapshots']['before'], normalized)
        sample = {'observer': view.get('observer'), 'driver': view.get('driver'),
                  'target': target, 'domain': domain}
        _validate_view(normalized, sample)
    except (KeyError, TypeError, IndexError) as error:
        raise ValueError('Incomplete retained PID-view proof') from error
    return normalized


def _derive_sample(sample, policy, own_root=None, retained=None):
    if sample.get('error'): return 'unknown'
    try:
        if sample.get('visibility') != {'ASCEND_VISIBLE_DEVICES': '', 'ASCEND_RT_VISIBLE_DEVICES': ''}: return 'unknown'
        target, domain, rows = _parsed(sample, policy)
        if target != sample['target'] or domain != sample['domain']: return 'unknown'
        _validate_view(policy, sample)
        if len(rows) != len(sample.get('processes', [])): return 'unknown'
        foreign = False
        for row, observed in zip(rows, sample['processes']):
            if observed.get('reported') != row or observed.get('error'): return 'unknown'
            identity = observed.get('identity')
            if not processes.valid_identity(identity): return 'unknown'
            # Local versus host mapping is part of the qualified driver view;
            # integer PID equality alone never grants ownership.
            view = policy['view_evidence']
            if (identity['birth']['boot_id'] != sample['observer']['birth']['boot_id']
                    or identity['pid_ns'] != sample['observer']['pid_ns']
                    or identity['mnt_ns'] != sample['observer']['mnt_ns']): return 'unknown'
            if view['kind'] == 'verified_local_pidns':
                if identity['pid'] != row['reported_pid']: return 'unknown'
            elif view['kind'] == 'host_proc':
                if identity['nspid'][0] != row['reported_pid']: return 'unknown'
            else: return 'unknown'
            lineage = observed.get('lineage')
            if own_root is None:
                foreign = True
            elif lineage and processes.validate_lineage(lineage, root_identity=own_root):
                if processes.identity_key(lineage[-1]) != processes.identity_key(identity): return 'unknown'
                if observed.get('lineage_source') == 'retained' and (retained or {}).get(processes.identity_key(identity)) != lineage:
                    return 'unknown'
                if observed.get('lineage_source') not in ('fresh', 'retained'): return 'unknown'
            else:
                foreign = True
        return 'foreign' if foreign else CLAIM
    except (ValueError, TypeError, KeyError, IndexError):
        return 'unknown'


def observe_target(policy, *, own_root=None, retained_lineages=None):
    policy = normalize_policy(policy)
    sample = {'schema_version': SAMPLE_SCHEMA, 'started_mono_ns': str(time.monotonic_ns()),
              'observed_ns': str(time.time_ns()), 'processes': [],
              'visibility': {key: os.environ.get(key, '') for key in ('ASCEND_VISIBLE_DEVICES', 'ASCEND_RT_VISIBLE_DEVICES')}}
    try:
        observer = processes.identify_process(os.getpid())
        sample['observer'] = observer
        if any(sample['visibility'].values()): raise ValueError('Ascend visibility remapping prevents physical isolation admission')
        binary = Path(shutil.which('npu-smi') or '/usr/local/Ascend/driver/tools/npu-smi').resolve(strict=True)
        deadline = time.monotonic() + policy['sample_timeout_ms'] / 1000
        for name, args in (('mapping', ['info', '-m']), ('inventory', ['info'])):
            result = subprocess.run([str(binary), *args], capture_output=True, timeout=max(.001, deadline-time.monotonic()))
            if len(result.stdout) + len(result.stderr) > MAX_BYTES: raise ValueError('Ascend observation exceeds retention bound')
            sample[name] = {'argv': [str(binary), *args], 'returncode': result.returncode,
                            'stdout': result.stdout.decode('utf-8', errors='strict'), 'stderr': result.stderr.decode('utf-8', errors='strict')}
        version = re.search(r'npu-smi\s+([^\s|]+)', sample['inventory']['stdout'])
        if version is None: raise ValueError('No Ascend driver identity in original inventory')
        sample.update(observer=observer, driver={'path': str(binary), 'sha256': digest(binary.read_bytes()), 'version_text': version.group(1)})
        target, domain, rows = _parsed(sample, policy)
        sample.update(target=target, domain=domain)
        _validate_view(policy, sample)
        for row in rows:
            record = {'reported': row}
            try:
                identity = processes.resolve_observed_pid(row['reported_pid'], policy['view_evidence'],
                    observer_context=observer, driver_identity=sample['driver'])
                if identity is None: raise ValueError('Reported process disappeared before identity capture')
                record['identity'] = identity
                retained = (retained_lineages or {}).get(processes.identity_key(identity))
                if own_root is not None and retained and processes.validate_lineage(retained, root_identity=own_root):
                    record.update(lineage=retained, lineage_source='retained')
                else:
                    record['lineage'] = processes.verify_lineage(own_root, identity['pid']) if own_root is not None else None
                    record['lineage_source'] = 'fresh'
            except (OSError, ValueError) as error:
                record['error'] = str(error)[:2048]
            sample['processes'].append(record)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        sample['error'] = type(error).__name__ + ': ' + str(error)[:2048]
    sample['ended_mono_ns'] = str(time.monotonic_ns())
    sample['ended_ns'] = str(time.time_ns())
    return sample


def _read(path):
    if path.resolve() != path: raise ValueError('Linked isolation evidence')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES: raise ValueError('Invalid isolation evidence file')
        return stream.read(MAX_BYTES + 1)


def _save(path, value):
    raw = canonical(value) + b'\n'
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    if path.exists() or path.is_symlink():
        temporary.unlink(); raise ValueError('Original isolation evidence already exists')
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
    return raw


def read_evidence(folder):
    folder = Path(folder)
    if folder.resolve() != folder: raise ValueError('Linked isolation attempt')
    result = {}
    for name, prefix in (('isolation-samples.jsonl', 'isolation_samples'), ('isolation-receipt.json', 'isolation_receipt'), ('admission.json', 'admission')):
        path = folder / name
        if path.exists() or path.is_symlink():
            if path.resolve() != path: raise ValueError('Linked isolation evidence')
            if path.stat().st_size > MAX_BYTES:
                result['isolation_evidence_error'] = 'Original isolation evidence exceeds the bounded retention limit'
                continue
            raw = _read(path)
            result[prefix + '_bytes'] = base64.b64encode(raw).decode()
            result[prefix + '_sha256'] = digest(raw)
    return result


def _bindings(request_raw, intent, policy):
    request = json.loads(request_raw)
    if request.get('schema_version') != 'kernelbridge.native-execution-request/v1':
        raise ValueError('Invalid original native execution request schema')
    bound = request['intent']
    for key in ('action_id', 'attempt', 'source_sha256'):
        if key not in intent or bound.get(key) != intent[key]: raise ValueError('Isolation original intent differs: ' + key)
    for key in ('mode', 'device'):
        if key in intent and bound.get(key) != intent[key]: raise ValueError('Isolation original intent differs: ' + key)
    normalized = normalize_policy(policy)
    if request.get('isolation_policy') != normalized: raise ValueError('Original isolation policy changed')
    if normalized['logical_device'] != bound['device']: raise ValueError('Isolation logical device differs from original request')
    pins = {key: request[key] for key in ('isolation_code_sha256', 'process_isolation_code_sha256', 'npu_enter_code_sha256')}
    if any(not isinstance(v, str) or re.fullmatch('[a-f0-9]{64}', v) is None for v in pins.values()): raise ValueError('Missing isolation code pin')
    if pins['process_isolation_code_sha256'] != processes.__source_sha256__:
        raise ValueError('Loaded process evidence helper differs from the original request')
    if pins['isolation_code_sha256'] != SELF_SHA256:
        raise ValueError('Loaded isolation observer differs from the original request')
    entry = Path(__file__).resolve().with_name('npu_enter.py')
    if entry.resolve() != entry or digest(entry.read_bytes()) != pins['npu_enter_code_sha256']:
        raise ValueError('Pinned SDK entry boundary differs from the original request')
    return request, {'intent': bound, 'execution_request_sha256': digest(request_raw),
                     'policy_sha256': policy_digest(normalized), **pins}


def _samples(raw):
    if not raw or not raw.endswith(b'\n'): raise ValueError('Incomplete original isolation samples')
    records = [json.loads(line) for line in raw.splitlines()]
    if any(row.get('sequence') != i or row.get('schema_version') != SAMPLE_SCHEMA for i, row in enumerate(records)):
        raise ValueError('Original isolation sample sequence changed')
    return records


def validate_admission(admission_raw, samples_raw, request_raw, *, intent, policy):
    request, binding = _bindings(request_raw, intent, policy)
    record = json.loads(admission_raw)
    if (record.get('schema_version') != ADMISSION_SCHEMA or record.get('status') != 'not_started'
            or record.get('dispatched') is not False or record.get('samples_sha256') != digest(samples_raw)
            or any(record.get(k) != v for k, v in binding.items())):
        raise ValueError('Original no-start admission binding changed')
    samples = _samples(samples_raw)
    if any(row.get('phase') not in {'admission', 'admission_locked'} for row in samples):
        raise ValueError('No-start admission contains dispatched samples')
    reason = record.get('reason_code')
    owner = record.get('supervisor', {})
    if (owner.get('code_sha256') != request.get('supervisor_sha256') or not samples
            or any(not processes.valid_identity(sample.get('observer'))
                   or sample['observer']['pid'] != owner.get('pid')
                   or sample['observer']['birth'] != owner.get('birth') for sample in samples)):
        raise ValueError('Original admission producer identity is incomplete or changed')
    if reason == 'cooperative_lease_busy':
        if (record.get('busy_device_key') not in device_keys(samples[-1])
                or record.get('lease_root') != normalize_policy(policy)['lease_root']
                or record.get('lease_scope') != SCOPE or _derive_sample(samples[-1], normalize_policy(policy)) != CLAIM):
            raise ValueError('Admission lease lacks a bound clear observation or original scope')
    elif reason in {'foreign', 'observation_unknown'}:
        expected = 'foreign' if reason == 'foreign' else 'unknown'
        if _derive_sample(samples[-1], normalize_policy(policy)) != expected: raise ValueError('Admission reason differs from raw observation')
    else: raise ValueError('Invalid no-start admission reason')
    if not isinstance(record.get('reason'), str) or not record['reason']: raise ValueError('Missing admission reason')
    return record


def validate_evidence(samples_raw, receipt_raw, request_raw, exit_raw, *, handle, intent, policy):
    request, binding = _bindings(request_raw, intent, policy)
    result = {'verdict': 'unknown', 'claim': CLAIM, 'scope': SCOPE, **binding,
              'source_sha256': intent['source_sha256'], 'action_id': intent['action_id'], 'attempt': intent['attempt'],
              'device': normalize_policy(policy)['logical_device'], 'coverage': {}, 'result_sha256': None}
    if not receipt_raw or not samples_raw or not exit_raw: return result
    record = json.loads(receipt_raw)
    supervisor = _module('npu_job_supervisor', request['supervisor_sha256'])
    if (record.get('schema_version') != RECEIPT_SCHEMA or not supervisor._same_handle(record.get('handle', {}), handle)
            or record.get('samples_sha256') != digest(samples_raw) or record.get('execution_exit_sha256') != digest(exit_raw)
            or any(record.get(k) != v for k, v in binding.items())):
        raise ValueError('Original isolation terminal binding changed')
    supervisor.validate_exit_receipt(exit_raw, handle=handle, intent=intent)
    supervisor.validate_launch_receipt(canonical(handle), request_raw, intent=intent)
    if handle.get('execution_request_sha256') != digest(request_raw): raise ValueError('Isolation handle/request changed')
    normalized = normalize_policy(policy)
    result.update(execution_exit_sha256=digest(exit_raw), result_sha256=record.get('result_sha256'))
    if result['result_sha256'] is not None and re.fullmatch('[a-f0-9]{64}', result['result_sha256']) is None:
        raise ValueError('Invalid original benchmark hash')
    try:
        rows = _samples(samples_raw)
        phases = [r['phase'] for r in rows]
        root = record.get('own_root')
        if not processes.valid_identity(root) or root['pid'] != handle['pid'] or root['birth'] != handle['process_birth']:
            return result
        derived = []; retained = {}
        for row in rows:
            verdict = _derive_sample(row, normalized, root if row['phase'] in {'running', 'final'} else None, retained)
            derived.append(verdict)
            if verdict != 'unknown':
                for item in row.get('processes', []):
                    if (item.get('lineage_source') == 'fresh'
                            and processes.validate_lineage(item.get('lineage'), root_identity=root)
                            and processes.identity_key(item['lineage'][-1]) == processes.identity_key(item['identity'])):
                        retained[processes.identity_key(item['identity'])] = item['lineage']
        starts = [int(row['started_mono_ns']) for row in rows]; ends = [int(row['ended_mono_ns']) for row in rows]
        clock_fields = ('started_mono_ns', 'ended_mono_ns', 'observed_ns', 'ended_ns')
        exact_clocks = all(isinstance(row.get(key), str) and re.fullmatch(r'[1-9][0-9]*', row[key]) for row in rows for key in clock_fields)
        wall_starts = [int(row['observed_ns']) for row in rows]; wall_ends = [int(row['ended_ns']) for row in rows]
        original_exit = json.loads(exit_raw)
        launch_ns, exit_ns = int(handle['started_ns']), int(original_exit['finished_ns'])
        gap = normalized['max_gap_ms'] * 1000000
        complete = (phases[:2] == ['admission', 'admission_locked'] and phases[-1] == 'final' and 'running' in phases
                    and exact_clocks and launch_ns <= exit_ns
                    and all(p == 'running' for p in phases[2:-1])
                    and all(0 <= b-a <= normalized['sample_timeout_ms'] * 1000000 for a, b in zip(starts, ends))
                    and all(0 <= b-a <= gap for a, b in zip(starts, starts[1:]))
                    and all(a <= b for a, b in zip(ends, starts[1:]))
                    and all(0 <= b-a <= normalized['sample_timeout_ms'] * 1000000 for a, b in zip(wall_starts, wall_ends))
                    and wall_ends[1] <= launch_ns <= wall_starts[2] <= launch_ns + gap
                    and exit_ns <= wall_starts[-1] <= exit_ns + gap
                    and all(a <= b for a, b in zip(wall_ends, wall_starts[1:])))
        keys = device_keys(rows[0])
        complete = complete and all(device_keys(row) == keys for row in rows)
        lease_records = record.get('leases', [])
        complete = complete and len(lease_records) == len(keys) and sorted(item['device_key'] for item in lease_records) == keys
        complete = complete and not record.get('observer_error')
        for row in rows[1:]:
            complete = complete and row.get('lease_evidence') == lease_records
        for lease in lease_records:
            complete = complete and (lease.get('schema') == 'kernelbridge.device-lease/v1'
                and lease.get('platform') == 'linux' and lease.get('boot_id') == root['birth']['boot_id']
                and lease.get('mnt_ns') == root['mnt_ns'] and lease.get('uid') == root['uid']
                and lease.get('shared_root') == normalized['lease_root'])
            complete = complete and (lease.get('root') == str(Path(lease['shared_root']) / str(lease['uid']))
                and lease.get('filename') == digest(lease['device_key'].encode()) + '.lock'
                and all(type(lease.get(field)) is int and lease[field] >= 0 for field in ('root_dev', 'root_ino', 'dev', 'ino'))
                and lease['root_ino'] > 0 and lease['ino'] > 0)
        result['coverage'] = {'samples': len(rows), 'phases': phases, 'bounded': bool(complete), 'device_keys': keys}
        result['verdict'] = 'foreign' if 'foreign' in derived else CLAIM if complete and all(v == CLAIM for v in derived) else 'unknown'
    except (ValueError, TypeError, KeyError, IndexError):
        result['verdict'] = 'unknown'
    return result


class Guard:
    def __init__(self, folder, request, request_sha):
        self.folder = Path(folder); self.request = request; self.request_sha = request_sha
        self.policy = normalize_policy(request['isolation_policy'])
        self.request_raw = _read(self.folder / 'execution-request.json')
        if digest(self.request_raw) != request_sha or json.loads(self.request_raw) != request: raise ValueError('Guard original request changed')
        _, self.binding = _bindings(self.request_raw, request['intent'], self.policy)
        self.leases = []; self.own_root = None; self.handle = None; self.sequence = 0
        self.stop = threading.Event(); self.thread = None
        self.failure = None
        self.retained_lineages = {}
        fd = os.open(self.folder / 'isolation-samples.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.stream = os.fdopen(fd, 'wb'); self.lock = threading.Lock()

    @property
    def inherited_fds(self): return tuple(lease.fd for lease in self.leases)

    def sample(self, phase, error=None):
        with self.lock:
            sample = observe_target(self.policy, own_root=self.own_root if phase in {'running', 'final'} else None,
                                    retained_lineages=self.retained_lineages)
            for item in sample.get('processes', []):
                if (not item.get('error') and item.get('lineage_source') == 'fresh'
                        and processes.validate_lineage(item.get('lineage'), root_identity=self.own_root)):
                    self.retained_lineages[processes.identity_key(item['identity'])] = item['lineage']
            sample['lease_evidence'] = []
            try:
                for lease in self.leases:
                    sample['lease_evidence'].append(processes.validate_lease(lease.fd, lease.evidence,
                        lease_root=self.policy['lease_root'], device_key=lease.evidence['device_key']))
            except (OSError, ValueError) as problem:
                error = 'Original shared lease could not be verified: ' + str(problem)
            if error is not None: sample['error'] = error
            sample.update(sequence=self.sequence, phase=phase)
            payload = canonical(sample) + b'\n'
            if len(payload) > MAX_SAMPLE_BYTES or (phase != 'final' and self.stream.tell() + len(payload) > MAX_BYTES - MAX_SAMPLE_BYTES):
                self.failure = 'Original isolation observation reached its bounded evidence capacity'
                self.stop.set()
                sample = {key: sample[key] for key in ('schema_version', 'sequence', 'phase', 'started_mono_ns', 'ended_mono_ns', 'observed_ns', 'ended_ns', 'observer') if key in sample}
                sample.update(error=self.failure, discarded_sample_bytes=len(payload), discarded_sample_sha256=digest(payload))
                payload = canonical(sample) + b'\n'
            self.sequence += 1
            self.stream.write(payload); self.stream.flush(); os.fsync(self.stream.fileno())
            return sample

    def _reject(self, sample, reason, code, busy_key=None):
        record = {'schema_version': ADMISSION_SCHEMA, 'status': 'not_started', 'dispatched': False,
                  **self.binding, 'reason': reason, 'reason_code': code,
                  'samples_sha256': digest(_read(self.folder / 'isolation-samples.jsonl')),
                  'supervisor': json.loads(_read(self.folder / 'supervisor.json'))}
        if busy_key is not None:
            record.update(busy_device_key=busy_key, lease_root=self.policy['lease_root'], lease_scope=SCOPE)
        _save(self.folder / 'admission.json', record)
        self.close()
        return {'allowed': False, 'admission': record, 'lease_evidence': []}

    def admit(self):
        sample = self.sample('admission'); verdict = _derive_sample(sample, self.policy)
        if verdict != CLAIM: return self._reject(sample, 'Original target occupancy is ' + verdict, 'foreign' if verdict == 'foreign' else 'observation_unknown')
        try:
            for key in device_keys(sample): self.leases.append(processes.acquire_lease(key, self.policy['lease_root']))
        except processes.LeaseBusy:
            return self._reject(sample, 'Original physical-device cooperative lease is busy', 'cooperative_lease_busy', key)
        except (OSError, ValueError) as error:
            sample = self.sample('admission_locked', error='Original shared lease admission failed: ' + str(error))
            return self._reject(sample, sample['error'], 'observation_unknown')
        sample_after = self.sample('admission_locked'); verdict = _derive_sample(sample_after, self.policy)
        if verdict != CLAIM or device_keys(sample_after) != device_keys(sample):
            return self._reject(sample_after, 'Original target occupancy changed during admission', 'foreign' if verdict == 'foreign' else 'observation_unknown')
        return {'allowed': True, 'lease_evidence': [lease.evidence for lease in self.leases]}

    def start(self, handle):
        self.handle = handle
        try:
            self.own_root = processes.identify_process(handle['pid'], require_live=False)
            if self.own_root is not None and self.own_root['birth'] != handle['process_birth']:
                self.own_root = None
        except (OSError, ValueError):
            self.own_root = None
        try:
            self.sample('running')
        except (OSError, ValueError) as error:
            self.failure = 'Initial isolation sample unavailable: ' + str(error)
        def watch():
            while not self.stop.wait(self.policy['sample_interval_ms'] / 1000):
                try:
                    self.sample('running')
                except (OSError, ValueError) as error:
                    self.failure = 'Isolation sample unavailable: ' + str(error)
                    return
        self.thread = threading.Thread(target=watch, name='native-device-observer', daemon=True)
        self.thread.start()

    def finish(self, exit_raw):
        self.stop.set()
        if self.thread is not None: self.thread.join()
        self.sample('final')
        result_path = self.folder / 'job/result.json'
        result_sha = digest(_read(result_path)) if result_path.exists() or result_path.is_symlink() else None
        record = {'schema_version': RECEIPT_SCHEMA, **self.binding, 'handle': self.handle,
                  'execution_exit_sha256': digest(exit_raw), 'result_sha256': result_sha,
                  'own_root': self.own_root, 'leases': [lease.evidence for lease in self.leases],
                  'observer_error': self.failure,
                  'samples_sha256': digest(_read(self.folder / 'isolation-samples.jsonl'))}
        raw = _save(self.folder / 'isolation-receipt.json', record)
        return validate_evidence(_read(self.folder / 'isolation-samples.jsonl'), raw, self.request_raw, exit_raw,
                                 handle=self.handle, intent=self.request['intent'], policy=self.policy)

    def close(self):
        self.stop.set()
        if self.thread is not None: self.thread.join()
        if not self.stream.closed: self.stream.close()
        for lease in self.leases: lease.close()
        self.leases = []
