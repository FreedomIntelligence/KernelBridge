"""Retain the real exit of one original native job; never infer or retry an exit.

Only the declared deployment root may be an alias. Everything below it is
ordinary evidence, including the attempt directory and the candidate.
"""
import base64
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time

EXIT_SCHEMA = 'kernelbridge.native-execution-exit/v1'
REQUEST_SCHEMA = 'kernelbridge.native-execution-request/v1'
HANDLE_SCHEMA = 'kernelbridge.native-execution-handle/v1'
INTENT_KEYS = ('action_id', 'attempt', 'source_sha256', 'mode', 'device')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _decimal(value):
    return isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]*', value) is not None


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _present(path):
    return path.exists() or path.is_symlink()


def _read(path):
    if path.resolve() != path:
        raise ValueError('Original evidence contains a link: ' + str(path))
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Original evidence is not a regular file')
        return stream.read()


def _save(path, value):
    raw = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    if path.exists() or path.is_symlink():
        temporary.unlink()
        raise ValueError('Original evidence already exists: ' + str(path))
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return raw


def _paths(remote_root, remote_attempt):
    declared_root, declared_attempt = Path(remote_root), Path(remote_attempt)
    if (not declared_root.is_absolute() or not declared_attempt.is_absolute()
            or '..' in declared_root.parts or '..' in declared_attempt.parts):
        raise ValueError('Original deployment paths must be absolute without traversal')
    relative = declared_attempt.relative_to(declared_root)
    if not relative.parts:
        raise ValueError('Original attempt must be below deployment root')
    root = declared_root.resolve(strict=True)
    folder = root / relative
    if folder.resolve() != folder:
        raise ValueError('Original attempt contains an internal link')
    return root, folder


def process_birth(pid, *, require_live=False):
    """Exact kernel process birth, not a command substring or rounded ps time."""
    if type(pid) is not int or pid <= 0:
        raise ValueError('Invalid process ID')
    if sys.platform.startswith('linux'):
        try:
            raw = (Path('/proc') / str(pid) / 'stat').read_text()
        except FileNotFoundError:
            return None
        fields = raw[raw.rfind(')') + 2:].split()
        if require_live and fields[0] in ('Z', 'X'):
            return None
        return {'platform': 'linux', 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'start_ticks': fields[19]}
    if sys.platform == 'darwin':
        # Darwin SDK sys/proc_info.h: proc_bsdinfo; libproc.h: proc_pidinfo.
        class BsdInfo(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint32) for name in (
                'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'rfu')]
            _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
            _fields_ += [(name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')]
            _fields_ += [('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64), ('start_usec', ctypes.c_uint64)]
        library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        library.proc_pidinfo.restype = ctypes.c_int
        info = BsdInfo()
        count = library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if count == 0 and ctypes.get_errno() in (0, 3):
            return None
        if count != ctypes.sizeof(info) or info.pid != pid:
            raise OSError('Cannot read exact Darwin process birth')
        if require_live and info.status == 5:  # SZOMB, SDK sys/proc.h.
            return None
        return {'platform': 'darwin', 'start_seconds': str(info.start_sec), 'start_microseconds': str(info.start_usec)}
    raise OSError('Exact process birth is unsupported on this platform')


def _valid_birth(value):
    if not isinstance(value, dict):
        return False
    if value.get('platform') == 'linux':
        return (isinstance(value.get('boot_id'), str)
                and re.fullmatch(r'[0-9a-f-]{36}', value['boot_id']) is not None
                and _decimal(value.get('start_ticks')))
    return (value.get('platform') == 'darwin' and _decimal(value.get('start_seconds'))
            and _decimal(value.get('start_microseconds')) and int(value['start_microseconds']) < 1000000)


def _same_handle(left, right):
    def normalized(value):
        result = dict(value)
        stamp = result.get('started_ns')
        if type(stamp) is int and stamp >= 0:
            stamp = str(stamp)
        if not _decimal(stamp):
            raise ValueError('Original handle timestamp is not exact')
        result['started_ns'] = stamp
        return result
    return json.dumps(normalized(left), sort_keys=True, allow_nan=False) == json.dumps(normalized(right), sort_keys=True, allow_nan=False)


def validate_launch_receipt(launch_raw, request_raw, *, intent):
    """Validate original launch bytes, including a lost-ACK read-only recovery."""
    handle, request = json.loads(launch_raw), json.loads(request_raw)
    if not isinstance(request, dict) or request.get('schema_version') != REQUEST_SCHEMA:
        raise ValueError('Invalid original execution request schema')
    bound = request.get('intent', {})
    if not isinstance(bound, dict) or any(key not in intent for key in ('action_id', 'attempt', 'source_sha256')):
        raise ValueError('Original launch identity is incomplete')
    for key in INTENT_KEYS:
        if key not in bound or (key in intent and bound[key] != intent[key]):
            raise ValueError('Original launch intent differs: ' + key)
    if (not isinstance(bound['action_id'], str) or not bound['action_id']
            or type(bound['attempt']) is not int or bound['attempt'] < 1
            or not _hash(bound['source_sha256']) or not isinstance(bound['mode'], str) or not bound['mode']
            or type(bound['device']) is not int or bound['device'] < 0):
        raise ValueError('Invalid original launch intent')
    for key in ('remote_root', 'remote_attempt', 'supervisor_sha256'):
        if key in intent and request.get(key) != intent[key]:
            raise ValueError('Original launch binding differs: ' + key)
    roots = [Path(request[key]) for key in ('remote_root', 'remote_attempt', 'canonical_root')]
    if any(not path.is_absolute() or '..' in path.parts for path in roots):
        raise ValueError('Invalid original deployment path')
    if not roots[1].relative_to(roots[0]).parts:
        raise ValueError('Original attempt must be below deployment root')
    argv = request.get('argv')
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or '\0' in v for v in argv):
        raise ValueError('Invalid original child argv')
    if not isinstance(handle, dict) or handle.get('schema_version') != HANDLE_SCHEMA:
        raise ValueError('Invalid original execution handle schema')
    if (handle.get('execution_request_sha256') != _sha(request_raw) or handle.get('argv') != argv
            or handle.get('attempt') != request['remote_attempt']
            or any(handle.get(key) != request[key] for key in ('remote_root', 'remote_attempt', 'canonical_root'))):
        raise ValueError('Original launch bytes differ from request binding')
    supervisor = handle.get('supervisor', {})
    if (not isinstance(supervisor, dict) or type(handle.get('pid')) is not int or handle['pid'] <= 0
            or not _valid_birth(handle.get('process_birth'))
            or type(supervisor.get('pid')) is not int or supervisor['pid'] <= 0
            or not _valid_birth(supervisor.get('birth')) or not _hash(supervisor.get('code_sha256'))
            or supervisor['code_sha256'] != request.get('supervisor_sha256')):
        raise ValueError('Original launch process binding is invalid')
    _same_handle(handle, handle)  # Reject malformed or rounded wall-clock stamps.
    return handle


def validate_exit_receipt(raw, *, handle, intent):
    """Pure validation of original bytes against independently retained identity.

    The caller retains the raw bytes and their SHA. This is an integrity contract,
    not a security boundary against another process with the same OS write rights.
    """
    record = json.loads(raw)
    if not isinstance(record, dict) or record.get('schema_version') != EXIT_SCHEMA:
        raise ValueError('Invalid native execution exit schema')
    bound = record.get('intent', {})
    for key in INTENT_KEYS:
        if key not in bound or (key in intent and bound[key] != intent[key]):
            raise ValueError('Original execution intent differs: ' + key)
    if any(key not in intent for key in ('action_id', 'attempt', 'source_sha256')):
        raise ValueError('Original execution identity is incomplete')
    if (not isinstance(bound['action_id'], str) or not bound['action_id']
            or type(bound['attempt']) is not int or bound['attempt'] < 1
            or not _hash(bound['source_sha256']) or not isinstance(bound['mode'], str)
            or type(bound['device']) is not int or bound['device'] < 0):
        raise ValueError('Invalid original execution intent')
    if handle.get('schema_version') != HANDLE_SCHEMA or not _same_handle(record.get('handle', {}), handle):
        raise ValueError('Original execution handle differs')
    supervisor = handle.get('supervisor', {})
    if (type(handle.get('pid')) is not int or handle['pid'] <= 0
            or not _valid_birth(handle.get('process_birth'))
            or type(supervisor.get('pid')) is not int or supervisor['pid'] <= 0
            or not _valid_birth(supervisor.get('birth')) or not _hash(supervisor.get('code_sha256'))
            or not _hash(handle.get('execution_request_sha256'))):
        raise ValueError('Original execution process binding is incomplete')
    if record.get('supervisor') != supervisor or record.get('process_birth') != handle['process_birth']:
        raise ValueError('Original supervisor or child birth differs')
    for key in ('remote_root', 'canonical_root', 'remote_attempt', 'execution_request_sha256'):
        if record.get(key) != handle.get(key) or not record.get(key):
            raise ValueError('Original execution binding differs: ' + key)
    if handle.get('attempt') != record['remote_attempt']:
        raise ValueError('Original execution attempt path differs')
    rc = record.get('returncode')
    if type(rc) is not int or not -255 <= rc <= 255:
        raise ValueError('Invalid actual child returncode')
    if record.get('signal') != (-rc if rc < 0 else None) or type(record.get('signal')) is bool:
        raise ValueError('Actual child signal differs from returncode')
    if not _decimal(record.get('finished_ns')):
        raise ValueError('Invalid execution finish time')
    return record


def _load(folder):
    # This first read discovers the declared root; all evidence is then reread
    # under its validated canonical path before it can grant any lifecycle state.
    initial = json.loads((Path(folder) / 'execution-request.json').read_bytes())
    root, canonical = _paths(initial['remote_root'], initial['remote_attempt'])
    if Path(folder) not in (canonical, Path(initial['remote_attempt'])) or Path(folder).resolve() != canonical:
        raise ValueError('Original execution folder differs')
    raw = _read(canonical / 'execution-request.json'); request = json.loads(raw)
    if request != initial or request.get('schema_version') != REQUEST_SCHEMA:
        raise ValueError('Original execution request changed')
    if request['canonical_root'] != str(root):
        raise ValueError('Original deployment root changed')
    if request['supervisor_sha256'] != _sha(Path(__file__).read_bytes()):
        raise ValueError('Original supervisor code changed')
    if json.loads(_read(canonical / 'intent.json')) != request['intent']:
        raise ValueError('Original execution intent changed')
    if _sha(_read(canonical / 'candidate.py')) != request['intent']['source_sha256']:
        raise ValueError('Original execution candidate changed')
    return canonical, request, _sha(raw)


def observe(folder, expected=None):
    folder, request, request_sha = _load(folder)
    if expected is not None:
        for key in ('action_id', 'attempt', 'source_sha256', 'mode', 'device'):
            if key in expected and request['intent'][key] != expected[key]:
                raise ValueError('Requested original execution identity differs')
        for key in ('remote_root', 'remote_attempt', 'supervisor_sha256'):
            if key in expected and request[key] != expected[key]:
                raise ValueError('Requested original execution binding differs: ' + key)
    launch_raw = _read(folder / 'launch.json') if _present(folder / 'launch.json') else None
    request_raw = _read(folder / 'execution-request.json')
    handle = validate_launch_receipt(launch_raw, request_raw, intent={**request['intent'], **(expected or {})}) if launch_raw is not None else None
    if handle is None:
        return {'handle': None, 'alive': False, 'lifecycle_state': 'unknown', 'identity_verified': False}
    if (handle.get('schema_version') != HANDLE_SCHEMA
            or handle.get('execution_request_sha256') != request_sha or handle.get('argv') != request['argv']
            or handle.get('attempt') != request['remote_attempt']
            or handle.get('remote_attempt') != request['remote_attempt']
            or handle.get('remote_root') != request['remote_root']
            or handle.get('canonical_root') != request['canonical_root']):
        raise ValueError('Original execution launch binding changed')
    if expected and expected.get('handle') is not None and not _same_handle(handle, expected['handle']):
        raise ValueError('Requested original handle changed')
    supervisor = json.loads(_read(folder / 'supervisor.json'))
    if handle.get('supervisor') != supervisor or supervisor.get('code_sha256') != request['supervisor_sha256']:
        raise ValueError('Original supervisor binding changed')
    if (type(handle.get('pid')) is not int or handle['pid'] <= 0 or not _valid_birth(handle.get('process_birth'))
            or type(supervisor.get('pid')) is not int or supervisor['pid'] <= 0 or not _valid_birth(supervisor.get('birth'))):
        raise ValueError('Original process birth binding is invalid')
    def terminal_receipt():
        if not _present(folder / 'execution-exit.json'):
            return None
        raw = _read(folder / 'execution-exit.json')
        record = validate_exit_receipt(raw, handle=handle, intent=request['intent'])
        return {'handle': handle, 'alive': False, 'lifecycle_state': 'exited', 'identity_verified': True,
                'original_launch_bytes': base64.b64encode(launch_raw).decode(), 'original_launch_sha256': _sha(launch_raw),
                'execution_request_bytes': base64.b64encode(request_raw).decode(), 'execution_request_sha256': request_sha,
                'execution_exit': record, 'execution_exit_sha256': _sha(raw),
                'execution_exit_bytes': base64.b64encode(raw).decode()}
    terminal = terminal_receipt()
    if terminal is not None:
        return terminal
    child_birth = process_birth(handle['pid'], require_live=True)
    child_alive = child_birth == handle.get('process_birth')
    supervisor_alive = process_birth(supervisor['pid'], require_live=True) == supervisor.get('birth')
    if not supervisor_alive:
        # The supervisor may have persisted its exit between the first receipt
        # read and this process snapshot. Prefer those original terminal bytes.
        terminal = terminal_receipt()
        if terminal is not None:
            return terminal
    # A matching supervisor may still be persisting the real wait receipt.
    # A different live PID birth is an identity mismatch, never settling.
    lifecycle = ('running' if child_alive and supervisor_alive else
                 'settling' if child_birth is None and supervisor_alive else 'unknown')
    return {'handle': handle, 'alive': child_alive, 'supervisor_alive': supervisor_alive, 'identity_verified': True,
            'original_launch_bytes': base64.b64encode(launch_raw).decode(), 'original_launch_sha256': _sha(launch_raw),
            'execution_request_bytes': base64.b64encode(request_raw).decode(), 'execution_request_sha256': request_sha,
            'lifecycle_state': lifecycle}


def launch(request, argv):
    """Create once; a repeated original identity only returns its original handle."""
    root, folder = _paths(request['remote_root'], request['remote_attempt'])
    original = {key: request[key] for key in INTENT_KEYS}
    data = base64.b64decode(request['source'], validate=True)
    if _sha(data) != original['source_sha256']:
        raise ValueError('Original candidate hash differs')
    if request.get('supervisor_sha256') != _sha(Path(__file__).read_bytes()):
        raise ValueError('Declared supervisor code differs')
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or '\0' in v for v in argv):
        raise ValueError('Original child argv is invalid')

    def existing():
        if json.loads(_read(folder / 'intent.json')) != original or _sha(_read(folder / 'candidate.py')) != original['source_sha256']:
            raise ValueError('Existing original attempt belongs to different input')
        if _present(folder / 'execution-request.json'):
            _, prior, _ = _load(folder)
            if (prior['argv'] != argv or prior['remote_root'] != request['remote_root']
                    or prior['remote_attempt'] != request['remote_attempt']):
                raise ValueError('Original child argv or declared deployment binding changed')
            return {'existing': True, 'handle': observe(folder, expected=request)['handle']}
        # A pre-supervisor or damaged attempt is still an irrevocable claim.
        # Do not turn its unbound file into a fresh trusted acknowledgement.
        return {'existing': True, 'handle': None}

    if folder.exists():
        return existing()
    lease = os.open(root / '.npu-job.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if folder.exists():
            return existing()
        folder.mkdir(parents=True, exist_ok=False)
        _save(folder / 'intent.json', original)
        candidate_fd = os.open(folder / 'candidate.py', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(candidate_fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        durable = {'schema_version': REQUEST_SCHEMA, 'intent': original, 'argv': argv,
                   'remote_root': request['remote_root'], 'canonical_root': str(root),
                   'remote_attempt': request['remote_attempt'], 'supervisor_sha256': request['supervisor_sha256']}
        raw = _save(folder / 'execution-request.json', durable)
        with (folder / 'supervisor.log').open('xb') as log:
            process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--supervise', str(folder), str(lease), _sha(raw)],
                                       cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True, pass_fds=(lease,))
            # Long-lived callers reap their own supervisor; short-lived SSH
            # launchers can exit without waiting for this daemon thread.
            threading.Thread(target=process.wait, name='native-supervisor-reaper', daemon=True).start()
    finally:
        os.close(lease)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (folder / 'launch.json').exists():
            return {'existing': False, 'handle': observe(folder)['handle']}
        time.sleep(.02)
    return {'existing': False, 'handle': None}


def _supervise(folder, lease, expected_sha):
    folder, request, request_sha = _load(folder)
    if expected_sha != request_sha:
        raise ValueError('Original supervisor request changed before startup')
    # The inherited open-file description keeps the lock across launcher exit;
    # the child also holds it if the supervisor is lost before waiting.
    lock_stat = os.fstat(lease)
    lock_path = Path(request['canonical_root']) / '.npu-job.lock'
    path_stat = lock_path.lstat()
    if (not stat.S_ISREG(path_stat.st_mode) or not stat.S_ISREG(lock_stat.st_mode)
            or (lock_stat.st_dev, lock_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)):
        raise ValueError('Original device lease changed')
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    supervisor = {'pid': os.getpid(), 'birth': process_birth(os.getpid()), 'code_sha256': request['supervisor_sha256']}
    _save(folder / 'supervisor.json', supervisor)
    with (folder / 'run.log').open('xb') as log:
        child = subprocess.Popen(request['argv'], cwd=request['remote_root'], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, start_new_session=True, pass_fds=(lease,))
    handle = {'schema_version': HANDLE_SCHEMA, 'pid': child.pid, 'argv': request['argv'], 'started_ns': str(time.time_ns()),
              'attempt': request['remote_attempt'], 'process_birth': process_birth(child.pid),
              'supervisor': supervisor, 'execution_request_sha256': request_sha,
              'remote_root': request['remote_root'], 'canonical_root': request['canonical_root'],
              'remote_attempt': request['remote_attempt']}
    if not _valid_birth(handle['process_birth']):
        # Never produce a qualified exit without exact child identity.
        child.wait()
        raise ValueError('Original child birth unavailable')
    _save(folder / 'launch.json', handle)
    rc = child.wait()
    record = {'schema_version': EXIT_SCHEMA, 'intent': request['intent'], 'handle': handle,
              'supervisor': supervisor, 'process_birth': handle['process_birth'],
              'returncode': rc, 'signal': -rc if rc < 0 else None, 'finished_ns': str(time.time_ns()),
              **{key: handle[key] for key in ('remote_root', 'canonical_root', 'remote_attempt', 'execution_request_sha256')}}
    raw = (json.dumps(record, allow_nan=False) + '\n').encode()
    validate_exit_receipt(raw, handle=handle, intent=request['intent'])
    # Revalidate original evidence after the child exits; tampering never gains
    # a trusted terminal receipt.
    _load(folder)
    _save(folder / 'execution-exit.json', record)
    os.close(lease)


if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == '--supervise':
        _supervise(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
    else:
        raise SystemExit('Internal supervisor entry point only')
