"""Exact process evidence and cooperative, scoped device leases (stdlib only).

PID-view records are host-admitted evidence, not model declarations.  Their raw
device snapshots must also be checked by the device-specific evidence consumer.
Lease scope is the declared shared filesystem, UID and mount namespace; a local
pathname alone does not establish exclusion in another container or filesystem.
"""
import ctypes
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time


class IsolationError(ValueError):
    """Evidence is unavailable, inconsistent or insufficient (unknown)."""


class LeaseBusy(IsolationError):
    """Another open file description holds this device lease."""


def _decimal(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9]+', value) is not None


def _positive(value):
    return type(value) is int and value > 0


def valid_birth(value):
    if not isinstance(value, dict):
        return False
    if value.get('platform') == 'linux':
        return (isinstance(value.get('boot_id'), str)
                and re.fullmatch(r'[0-9a-f-]{36}', value['boot_id']) is not None
                and _decimal(value.get('start_ticks')))
    return (value.get('platform') == 'darwin' and _decimal(value.get('start_seconds'))
            and _decimal(value.get('start_microseconds')) and int(value['start_microseconds']) < 1000000)


def valid_identity(value):
    if (not isinstance(value, dict) or not _positive(value.get('pid'))
            or type(value.get('ppid')) is not int or value['ppid'] < 0
            or type(value.get('uid')) is not int or value['uid'] < 0
            or not valid_birth(value.get('birth'))):
        return False
    if value['birth']['platform'] == 'linux':
        return (isinstance(value.get('pid_ns'), str) and re.fullmatch(r'pid:\[[0-9]+\]', value['pid_ns']) is not None
                and isinstance(value.get('mnt_ns'), str) and re.fullmatch(r'mnt:\[[0-9]+\]', value['mnt_ns']) is not None
                and isinstance(value.get('nspid'), list) and bool(value['nspid'])
                and all(_positive(pid) for pid in value['nspid']) and value['nspid'][0] == value['pid'])
    return value.get('pid_ns') is None and value.get('mnt_ns') is None and value.get('nspid') == [value['pid']]


def identity_key(value):
    """Stable process identity; PPid and process state can change after birth."""
    if not valid_identity(value):
        raise IsolationError('Invalid exact process identity')
    birth = value['birth']
    return (value['pid'], tuple(sorted(birth.items())), value['pid_ns'], value['mnt_ns'], tuple(value['nspid']))


def _linux_stat(path, pid):
    raw = path.read_text()
    end = raw.rfind(')')
    if end < 0 or raw.split(' ', 1)[0] != str(pid):
        raise IsolationError('Malformed process stat identity')
    fields = raw[end + 2:].split()
    if len(fields) < 20 or not fields[1].isdigit() or not fields[19].isdigit():
        raise IsolationError('Incomplete process stat identity')
    return fields[0], int(fields[1]), fields[19]


def _darwin_info(pid):
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
    if count == 0 and ctypes.get_errno() in (0, errno.ESRCH):
        return None
    if count != ctypes.sizeof(info) or info.pid != pid:
        raise IsolationError('Cannot read exact Darwin process identity')
    return info


def identify_process(pid, *, proc_root='/proc', require_live=True):
    """Return exact identity, None for an absent/dead PID, or raise for unknown.

    Linux stat/status/namespace reads are bracketed by matching birth and PPid
    reads.  Namespace links are kernel metadata, deliberately read with readlink.
    A single NSpid is valid metadata; it is not itself proof of a driver PID view.
    """
    if not _positive(pid):
        raise IsolationError('Invalid process ID')
    if sys.platform.startswith('linux') or str(proc_root) != '/proc':
        root = Path(proc_root)
        folder = root / str(pid)
        try:
            before = _linux_stat(folder / 'stat', pid)
            status = {}
            for line in (folder / 'status').read_text().splitlines():
                key, separator, value = line.partition(':')
                if separator:
                    if key in status:
                        raise IsolationError('Duplicate process status field')
                    status[key] = value.split()
            result = {'pid': pid, 'ppid': before[1], 'uid': int(status['Uid'][0]),
                      'birth': {'platform': 'linux', 'boot_id': (root / 'sys/kernel/random/boot_id').read_text().strip(),
                                'start_ticks': before[2]},
                      'pid_ns': os.readlink(folder / 'ns/pid'), 'mnt_ns': os.readlink(folder / 'ns/mnt'),
                      'nspid': [int(part) for part in status['NSpid']], 'state': before[0]}
            after = _linux_stat(folder / 'stat', pid)
            if (before[1:] != after[1:] or status['Pid'] != [str(pid)]
                    or status['PPid'] != [str(before[1])] or not valid_identity(result)
                    or result['pid_ns'] != os.readlink(folder / 'ns/pid')
                    or result['mnt_ns'] != os.readlink(folder / 'ns/mnt')):
                raise IsolationError('Process identity changed or has incomplete namespace evidence')
            if require_live and (before[0] in ('Z', 'X') or after[0] in ('Z', 'X')):
                return None
            if pid == os.getpid():
                # Strengthen observer scope when this proc view exposes its own
                # namespace's PID 1; do not guess a PID in another namespace.
                try:
                    init_before = _linux_stat(root / '1/stat', 1)
                    init_ns = os.readlink(root / '1/ns/pid')
                    init_after = _linux_stat(root / '1/stat', 1)
                    if (init_ns == result['pid_ns'] and init_before[2] == init_after[2]
                            and init_ns == os.readlink(root / '1/ns/pid')):
                        result['pid_ns_init_birth'] = {**result['birth'], 'start_ticks': init_after[2]}
                except (OSError, IsolationError):
                    pass  # The existing boot/namespace scope remains explicit.
            return result
        except FileNotFoundError:
            return None
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise IsolationError('Cannot read complete exact process identity') from error
    if sys.platform == 'darwin':
        info = _darwin_info(pid)
        if info is None or (require_live and info.status == 5):
            return None
        return {'pid': pid, 'ppid': info.ppid, 'uid': info.uid,
                'birth': {'platform': 'darwin', 'start_seconds': str(info.start_sec),
                          'start_microseconds': str(info.start_usec)},
                'pid_ns': None, 'mnt_ns': None, 'nspid': [pid], 'state': str(info.status)}
    raise IsolationError('Exact process identity is unsupported on this platform')


def process_birth(pid, *, require_live=False):
    """The existing supervisor birth format, without rounding kernel fields."""
    identity = identify_process(pid, require_live=require_live)
    return None if identity is None else identity['birth']


def wait_child_exit(pid, *, nonblocking=False):
    """Observe one direct Linux child exit without releasing its kernel PID."""
    if not sys.platform.startswith('linux') or not _positive(pid):
        raise IsolationError('Non-reaping child observation requires Linux')
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise IsolationError('A SIGCHLD handler prevents an exclusive child reaping fence')
    flags = os.WEXITED | os.WNOWAIT | (os.WNOHANG if nonblocking else 0)
    record = {'schema': 'kernelbridge.child-wait-observation/v1', 'pid': pid, 'parent_pid': os.getpid(),
              'started_mono_ns': str(time.monotonic_ns()), 'started_ns': str(time.time_ns()),
              'options': ['WEXITED', 'WNOWAIT'] + (['WNOHANG'] if nonblocking else [])}
    observed = os.waitid(os.P_PID, pid, flags)
    if observed is None: return None
    record.update(result={name: getattr(observed, name) for name in ('si_pid', 'si_uid', 'si_signo', 'si_status', 'si_code')},
                  ended_mono_ns=str(time.monotonic_ns()), ended_ns=str(time.time_ns()))
    validate_child_wait(record, pid=pid, parent_pid=os.getpid())
    return record


def validate_child_wait(record, *, pid, parent_pid):
    """Pure interpretation of a recorded Linux waitid(WEXITED|WNOWAIT)."""
    if (not isinstance(record, dict) or not _positive(pid) or not _positive(parent_pid)
            or record.get('schema') != 'kernelbridge.child-wait-observation/v1' or record.get('pid') != pid
            or record.get('parent_pid') != parent_pid or record.get('options') not in
            (['WEXITED', 'WNOWAIT'], ['WEXITED', 'WNOWAIT', 'WNOHANG'])):
        raise IsolationError('Original non-reaping child wait changed')
    for start, end in (('started_mono_ns', 'ended_mono_ns'), ('started_ns', 'ended_ns')):
        if not all(_decimal(record.get(k)) and int(record[k]) > 0 for k in (start, end)) or int(record[end]) < int(record[start]):
            raise IsolationError('Original child wait clock is invalid')
    value = record.get('result')
    if (not isinstance(value, dict) or any(type(value.get(k)) is not int for k in ('si_pid', 'si_uid', 'si_signo', 'si_status', 'si_code'))
            or value['si_pid'] != pid or value['si_uid'] < 0 or value['si_signo'] != 17):
        raise IsolationError('Original Linux child wait result differs')
    # Linux siginfo CLD_EXITED/KILLED/DUMPED; never infer an exit from a PID.
    if value['si_code'] == 1 and 0 <= value['si_status'] <= 255: return value['si_status']
    if value['si_code'] in (2, 3) and 0 < value['si_status'] < 65: return -value['si_status']
    raise IsolationError('Original wait did not report a terminal child exit')


def observe_unreaped_child(identity):
    """Retain raw zombie identity and a non-reaping OS wait for a direct child."""
    if not valid_identity(identity) or identity['ppid'] != os.getpid():
        raise IsolationError('Exit fence applies only to the original direct child')
    folder = Path('/proc') / str(identity['pid'])
    record = {'schema': 'kernelbridge.unreaped-child/v1', 'started_mono_ns': str(time.monotonic_ns()),
              'started_ns': str(time.time_ns()), 'stat_before': (folder / 'stat').read_text()}
    record['wait'] = wait_child_exit(identity['pid'], nonblocking=True)
    if record['wait'] is None: raise IsolationError('Original child has no waitable OS exit')
    record.update(stat_after=(folder / 'stat').read_text(), status_after=(folder / 'status').read_text(),
                  boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    record['namespaces_after'] = {}
    for name in ('pid', 'mnt'):
        try: record['namespaces_after'][name] = os.readlink(folder / 'ns' / name)
        except FileNotFoundError: record['namespaces_after'][name] = None
    record.update(ended_mono_ns=str(time.monotonic_ns()), ended_ns=str(time.time_ns()))
    validate_unreaped_child(record, identity, parent_pid=os.getpid())
    return record


def validate_unreaped_child(record, identity, *, parent_pid):
    """A pre-inventory live identity plus an unreaped kernel child, not a cache."""
    if (record.get('schema') != 'kernelbridge.unreaped-child/v1' or not valid_identity(identity)
            or identity['birth']['platform'] != 'linux' or identity['ppid'] != parent_pid
            or record.get('boot_id') != identity['birth']['boot_id']):
        raise IsolationError('Original unreaped process binding changed')
    for name in ('stat_before', 'stat_after'):
        raw = record[name]; end = raw.rfind(')'); fields = raw[end + 2:].split()
        if (end < 0 or raw.split(' ', 1)[0] != str(identity['pid']) or len(fields) < 20
                or fields[1] != str(parent_pid) or fields[19] != identity['birth']['start_ticks']
                or name == 'stat_after' and fields[0] not in ('Z', 'X')):
            raise IsolationError('Original unreaped kernel PID or birth changed')
    status = {}
    for line in record['status_after'].splitlines():
        key, sep, value = line.partition(':')
        if sep:
            if key in status: raise IsolationError('Duplicate terminal process status field')
            status[key] = value.split()
    if (status.get('Pid') != [str(identity['pid'])] or status.get('PPid') != [str(parent_pid)]
            or status.get('NSpid') != [str(pid) for pid in identity['nspid']]
            or not status.get('Uid') or status['Uid'][0] != str(identity['uid'])):
        raise IsolationError('Original terminal process identity differs')
    for name in ('pid', 'mnt'):
        if record['namespaces_after'].get(name) not in (None, identity[name + '_ns']):
            raise IsolationError('Original terminal namespace differs')
    rc = validate_child_wait(record['wait'], pid=identity['pid'], parent_pid=parent_pid)
    if record['wait']['result']['si_uid'] != identity['uid']:
        raise IsolationError('Original terminal process UID differs')
    for start, end in (('started_mono_ns', 'ended_mono_ns'), ('started_ns', 'ended_ns')):
        if (not all(_decimal(record.get(k)) and int(record[k]) > 0 for k in (start, end))
                or not int(record[start]) <= int(record['wait'][start]) <= int(record['wait'][end]) <= int(record[end])):
            raise IsolationError('Original terminal observation clock differs')
    return rc


def validate_lineage(lineage, root_identity=None):
    """Pure validation of a retained root-to-target, exact-birth evidence chain.

    This validates recorded evidence, not current liveness. Only the host reader
    may create these records. Reparented processes retain a previously proven
    chain, never gain ownership from their new parent, UID or executable name.
    """
    if not isinstance(lineage, list) or not lineage or not all(valid_identity(item) for item in lineage):
        return False
    if root_identity is not None and (not valid_identity(root_identity)
                                     or identity_key(lineage[0]) != identity_key(root_identity)):
        return False
    if len({item['pid'] for item in lineage}) != len(lineage):
        return False
    for parent, child in zip(lineage, lineage[1:]):
        if child['ppid'] != parent['pid'] or child['birth']['platform'] != parent['birth']['platform']:
            return False
        if parent['birth']['platform'] == 'linux':
            if (parent['birth']['boot_id'] != child['birth']['boot_id']
                    or int(parent['birth']['start_ticks']) > int(child['birth']['start_ticks'])):
                return False
        elif (int(parent['birth']['start_seconds']), int(parent['birth']['start_microseconds'])) > (
                int(child['birth']['start_seconds']), int(child['birth']['start_microseconds'])):
            return False
    return True


def verify_lineage(root_identity, pid, *, proc_root='/proc'):
    """Read and recheck every PPid/birth edge. None means verified non-descendant.

    Missing ancestors and PID reuse are unknown (raise), not foreign. A retained
    successful chain can later prove a descendant after it has been reparented.
    """
    identity_key(root_identity)
    if not _positive(pid):
        raise IsolationError('Invalid process ancestry target')
    chain, seen = [], set()
    current = pid
    while current:
        if current in seen:
            raise IsolationError('Process ancestry contains a cycle')
        seen.add(current)
        item = identify_process(current, proc_root=proc_root)
        if item is None:
            raise IsolationError('Process ancestry disappeared before verification')
        chain.append(item)
        if current == root_identity['pid']:
            if identity_key(item) != identity_key(root_identity):
                raise IsolationError('Root process PID was reused')
            break
        current = item['ppid']
    for item in chain:
        fresh = identify_process(item['pid'], proc_root=proc_root)
        if fresh is None or identity_key(fresh) != identity_key(item) or fresh['ppid'] != item['ppid']:
            raise IsolationError('Process ancestry changed during verification')
    if identity_key(chain[-1]) != identity_key(root_identity):
        return None
    chain.reverse()
    if not validate_lineage(chain, root_identity):
        raise IsolationError('Invalid process ancestry evidence')
    return chain


def classify_process(identity, lineages):
    """Classify against host-verified retained chains; unavailable is unknown."""
    if not valid_identity(identity) or not isinstance(lineages, list) or not lineages:
        return 'unknown'
    if not all(validate_lineage(chain) for chain in lineages):
        return 'unknown'
    return 'own' if any(identity_key(identity) == identity_key(item) for chain in lineages for item in chain) else 'foreign'


def _valid_driver(driver):
    return (isinstance(driver, dict) and isinstance(driver.get('path'), str)
            and Path(driver['path']).is_absolute() and isinstance(driver.get('sha256'), str)
            and re.fullmatch(r'[0-9a-f]{64}', driver['sha256']) is not None
            and isinstance(driver.get('version_text'), str) and bool(driver['version_text']))


def validate_view_evidence(view, *, observer_context, driver_identity):
    """Check a host-admitted probe's exact scope and PID correlation, purely.

    The caller must additionally validate evidence_sha256 and these PID sets
    against the original device-specific probe bytes before admitting the view.
    """
    if (not isinstance(view, dict) or view.get('schema') != 'kernelbridge.process-view/v1'
            or view.get('kind') not in ('host_proc', 'verified_local_pidns')
            or not valid_identity(view.get('observer')) or not valid_identity(observer_context)
            or not _valid_driver(view.get('driver')) or view['driver'] != driver_identity
            or not isinstance(view.get('device_key'), str) or not view['device_key']):
        raise IsolationError('Missing or invalid admitted process PID-view evidence')
    previous = view['observer']
    if (previous['birth']['platform'] != 'linux' or observer_context['birth']['platform'] != 'linux'
            or any(previous[key] != observer_context[key] for key in ('pid_ns', 'mnt_ns'))
            or previous['birth']['boot_id'] != observer_context['birth']['boot_id']
            or previous.get('pid_ns_init_birth') != observer_context.get('pid_ns_init_birth')
            or ('pid_ns_init_birth' in previous and not valid_birth(previous['pid_ns_init_birth']))):
        raise IsolationError('Process PID-view scope changed')
    probe = view.get('probe')
    if (not isinstance(probe, dict) or not valid_identity(probe.get('process'))
            or not valid_identity(probe.get('active_process'))
            or identity_key(probe['process']) != identity_key(probe['active_process'])
            or not _positive(probe.get('reported_pid')) or type(probe.get('exit_code')) is not int
            or probe['exit_code'] != 0 or not isinstance(probe.get('evidence_sha256'), str)
            or re.fullmatch(r'[0-9a-f]{64}', probe['evidence_sha256']) is None
            or any(not isinstance(probe.get(key), list) or not all(_positive(pid) for pid in probe[key])
                   for key in ('before_pids', 'active_pids', 'after_pids'))):
        raise IsolationError('Incomplete process PID-view probe evidence')
    process, reported = probe['process'], probe['reported_pid']
    if (process['birth'].get('boot_id') != previous['birth']['boot_id']
            or process['mnt_ns'] != previous['mnt_ns']
            or reported in probe['before_pids'] or reported not in probe['active_pids']
            or reported in probe['after_pids'] or reported != process['pid'] or reported != process['nspid'][0]):
        raise IsolationError('Process PID-view probe does not establish an exact PID correlation')
    if view['kind'] == 'verified_local_pidns' and process['pid_ns'] != previous['pid_ns']:
        raise IsolationError('Local PID-view proof crosses unverified namespaces')


def resolve_observed_pid(pid, view_evidence, *, proc_root='/proc', observer_context=None, driver_identity=None):
    """Resolve only a proven proc-root PID view; never infer PID offsets.

    host_proc is explicitly admitted by the host; local view additionally requires
    the observed process to remain in the proven PID namespace. Unsupported view
    translations are unknown, including a bare or missing NSpid declaration.
    """
    if observer_context is None:
        observer_context = identify_process(os.getpid(), proc_root=proc_root)
    if driver_identity is None:
        supplied = view_evidence.get('driver') if isinstance(view_evidence, dict) else None
        if not _valid_driver(supplied):
            raise IsolationError('Missing driver identity for PID-view verification')
        driver_identity = dict(supplied)
        driver_identity['sha256'] = hashlib.sha256(Path(supplied['path']).read_bytes()).hexdigest()
    validate_view_evidence(view_evidence, observer_context=observer_context, driver_identity=driver_identity)
    identity = identify_process(pid, proc_root=proc_root)
    if identity is None:
        return None
    if (identity['birth'].get('boot_id') != observer_context['birth']['boot_id']
            or identity['nspid'][0] != pid
            or (view_evidence['kind'] == 'verified_local_pidns' and identity['pid_ns'] != observer_context['pid_ns'])):
        raise IsolationError('Observed PID is outside the proven process view')
    return identity


class Lease:
    """One inherited open file description. Closing never issues LOCK_UN."""
    def __init__(self, fd, evidence):
        self.fd, self.evidence = fd, evidence

    def detach(self):
        if self.fd is None:
            raise IsolationError('Device lease is already closed or detached')
        fd, self.fd = self.fd, None
        return fd

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _scope():
    current = identify_process(os.getpid())
    if current is None:
        raise IsolationError('Cannot identify lease owner scope')
    return {'uid': os.getuid(), 'mnt_ns': current['mnt_ns'],
            'boot_id': current['birth'].get('boot_id'), 'platform': current['birth']['platform']}


def _root(lease_root):
    declared = Path(lease_root)
    if not declared.is_absolute() or '..' in declared.parts or declared.name in ('', '.', '..'):
        raise IsolationError('Device lease root must be an absolute declared directory')
    parent = declared.parent.resolve(strict=True)
    shared = parent / declared.name
    created = False
    try:
        shared.mkdir(mode=0o1777)
        created = True
    except FileExistsError:
        pass
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    base = os.open(shared, flags)
    root_fd = None
    try:
        info = os.fstat(base)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                or ((info.st_mode & 0o022) and not (info.st_mode & stat.S_ISVTX))):
            raise IsolationError('Unsafe shared device lease directory owner or mode')
        if created:
            if info.st_uid != os.getuid():
                raise IsolationError('Shared device lease directory changed during creation')
            os.fchmod(base, 0o1777)
        name = str(os.getuid())
        try:
            os.mkdir(name, 0o700, dir_fd=base)
        except FileExistsError:
            pass
        root_fd = os.open(name, flags, dir_fd=base)
        scoped = os.fstat(root_fd)
        linked = os.stat(name, dir_fd=base, follow_symlinks=False)
        base_link = shared.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(scoped.st_mode) or scoped.st_uid != os.getuid() or scoped.st_mode & 0o077
                or (linked.st_dev, linked.st_ino) != (scoped.st_dev, scoped.st_ino)
                or (base_link.st_dev, base_link.st_ino) != (info.st_dev, info.st_ino)):
            raise IsolationError('Unsafe or replaced UID-scoped device lease directory')
        result = shared, shared / name, root_fd, scoped
        root_fd = None
        return result
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(base)


def _lock_info(fd, root_fd, filename):
    info, linked = os.fstat(fd), os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(linked.st_mode)
            or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077
            or (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino)):
        raise IsolationError('Device lease file owner, type or inode is unsafe')
    return info


def _check_root_link(root, expected):
    linked = root.stat(follow_symlinks=False)
    if (not stat.S_ISDIR(linked.st_mode) or linked.st_uid != expected.st_uid
            or (linked.st_dev, linked.st_ino) != (expected.st_dev, expected.st_ino)):
        raise IsolationError('Device lease root changed while acquiring or validating the lock')


def acquire_lease(device_key, lease_root, *, expected_scope=None):
    """Acquire one physical-device key below the declared root's actual UID.

    The file is persistent: never truncated, renamed or unlinked. All launchers
    must declare the same shared root and physical key. Inherit fd with pass_fds;
    release by closing the last inherited holder, never by explicit LOCK_UN.
    """
    if not isinstance(device_key, str) or not device_key or len(device_key.encode()) > 4096:
        raise IsolationError('Invalid physical device lease key')
    shared, root, root_fd, root_info = _root(lease_root)
    fd = None
    try:
        scope = _scope()
        if expected_scope is not None and (not isinstance(expected_scope, dict)
                                          or any(key not in scope or scope[key] != value for key, value in expected_scope.items())):
            raise IsolationError('Device lease scope differs from the declared scope')
        filename = hashlib.sha256(device_key.encode()).hexdigest() + '.lock'
        fd = os.open(filename, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0), 0o600, dir_fd=root_fd)
        _lock_info(fd, root_fd, filename)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise LeaseBusy('Physical device lease is busy') from error
            raise
        info = _lock_info(fd, root_fd, filename)
        _check_root_link(root, root_info)
        evidence = {'schema': 'kernelbridge.device-lease/v1', 'device_key': device_key,
                    'shared_root': str(shared), 'root': str(root), 'filename': filename,
                    'root_dev': root_info.st_dev, 'root_ino': root_info.st_ino,
                    'dev': info.st_dev, 'ino': info.st_ino, **scope}
        result, fd = Lease(fd, evidence), None
        return result
    finally:
        if fd is not None:
            os.close(fd)
        os.close(root_fd)


def validate_lease(fd, evidence, *, lease_root, device_key):
    """Validate a still-held inherited FD; never reacquire and mask a lock gap.

    Linux fdinfo identifies the held FLOCK on this open file description. Other
    platforms can acquire leases but cannot make this inherited-lock proof.
    """
    if type(fd) is not int or fd < 0 or not isinstance(evidence, dict):
        raise IsolationError('Invalid inherited lease descriptor')
    shared, root, root_fd, root_info = _root(lease_root)
    try:
        filename = hashlib.sha256(device_key.encode()).hexdigest() + '.lock'
        info = _lock_info(fd, root_fd, filename)
        _check_root_link(root, root_info)
        actual = {'schema': 'kernelbridge.device-lease/v1', 'device_key': device_key,
                  'shared_root': str(shared), 'root': str(root), 'filename': filename,
                  'root_dev': root_info.st_dev, 'root_ino': root_info.st_ino,
                  'dev': info.st_dev, 'ino': info.st_ino, **_scope()}
        if evidence != actual:
            raise IsolationError('Inherited device lease scope or inode changed')
        if not sys.platform.startswith('linux'):
            raise IsolationError('Inherited flock proof is unavailable on this platform')
        locks = (Path('/proc/self/fdinfo') / str(fd)).read_text().splitlines()
        if not any(re.match(r'lock:\s+\S+:\s+FLOCK\s+ADVISORY\s+WRITE\s+', line) for line in locks):
            raise IsolationError('Inherited device lease FD does not hold an exclusive flock')
        return dict(actual)
    finally:
        os.close(root_fd)
