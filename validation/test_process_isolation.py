"""Portable process evidence fixtures and real local cooperative flock tests.

Linux /proc fixtures test parsing/proofs, not a claim of Linux execution. The
explicit Linux-only fdinfo test exercises that kernel path when run on Linux.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / 'examples/operator_pipeline/process_isolation.py'
SPEC = importlib.util.spec_from_file_location('process_isolation_under_test', SOURCE)
isolation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(isolation)
BOOT = '12345678-1234-1234-1234-123456789abc'
DEVICE = BOOT + ':accelerator:0000:9f:00.0:1'


class ProcFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kernelbridge-process-evidence-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.proc = self.base / 'proc'
        (self.proc / 'sys/kernel/random').mkdir(parents=True)
        (self.proc / 'sys/kernel/random/boot_id').write_text(BOOT + '\n')
        self.write_process(1, ppid=0, ticks='1')

    def write_process(self, pid, *, ppid=1, ticks='100', ns='pid:[41]', mount='mnt:[42]',
                      nspid=None, uid=501, state='S'):
        folder = self.proc / str(pid)
        (folder / 'ns').mkdir(parents=True, exist_ok=True)
        # /proc stat field 22 is the exact decimal start tick. Comm deliberately
        # contains spaces and closing parentheses; split-on-whitespace is wrong.
        fields = [state, str(ppid)] + ['0'] * 17 + [ticks] + ['0'] * 8
        (folder / 'stat').write_text(str(pid) + ' (same (process)) name) ' + ' '.join(fields) + '\n')
        (folder / 'status').write_text(
            f'Name:\tsame-command\nPid:\t{pid}\nPPid:\t{ppid}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n'
            + 'NSpid:\t' + '\t'.join(str(value) for value in (nspid or [pid])) + '\n')
        for name, target in (('pid', ns), ('mnt', mount)):
            link = folder / 'ns' / name
            if link.is_symlink():
                link.unlink()
            link.symlink_to(target)

    def identity(self, pid):
        return isolation.identify_process(pid, proc_root=self.proc)

    def tree(self):
        self.write_process(10, ticks='10')
        self.write_process(20, ppid=10, ticks='20')
        self.write_process(30, ppid=20, ticks='30')
        self.write_process(40, ticks='40')  # Same UID/name; unrelated ancestry.
        return self.identity(10)

    def view(self):
        self.write_process(70, ticks='70')
        self.write_process(80, ppid=70, ticks='80')
        driver_path = self.base / 'driver'
        driver_path.write_bytes(b'fixture driver identity\n')
        driver = {'path': str(driver_path), 'sha256': hashlib.sha256(driver_path.read_bytes()).hexdigest(),
                  'version_text': 'fixture-1'}
        observer, process = self.identity(70), self.identity(80)
        value = {'schema': 'kernelbridge.process-view/v1', 'kind': 'verified_local_pidns',
                 'observer': observer, 'driver': driver, 'device_key': DEVICE,
                 'probe': {'process': process, 'active_process': copy.deepcopy(process),
                           'reported_pid': 80, 'before_pids': [], 'active_pids': [80],
                           'after_pids': [], 'exit_code': 0, 'evidence_sha256': 'a' * 64}}
        return value, observer, driver


class ProcessIdentityTests(ProcFixture):
    def test_exact_birth_comm_and_namespace_fields(self):
        ticks = '900719925474099312345'
        self.write_process(10, ticks=ticks, nspid=[10, 3])
        value = self.identity(10)
        self.assertEqual(value['birth'], {'platform': 'linux', 'boot_id': BOOT, 'start_ticks': ticks})
        self.assertEqual((value['pid'], value['ppid'], value['uid']), (10, 1, 501))
        self.assertEqual((value['pid_ns'], value['mnt_ns'], value['nspid']), ('pid:[41]', 'mnt:[42]', [10, 3]))
        self.assertTrue(isolation.valid_identity(value))

    def test_absent_and_zombie_are_not_live(self):
        self.assertIsNone(self.identity(999))
        self.write_process(10, state='Z')
        self.assertIsNone(self.identity(10))
        self.assertEqual(isolation.identify_process(10, proc_root=self.proc, require_live=False)['state'], 'Z')

    def test_pid_and_namespace_metadata_fail_closed(self):
        for pid in (True, False, 0, -1, '10', None):
            with self.subTest(pid=pid), self.assertRaises(isolation.IsolationError):
                self.identity(pid)
        for status in ('Name:\tx\nPid:\t10\nPPid:\t1\nUid:\t501\n',
                       'Pid:\t10\nPPid:\t1\nUid:\t501\nNSpid:\t11\n',
                       'Pid:\t10\nPPid:\t1\nUid:\t501\nNSpid:\t10\nNSpid:\t10\n'):
            self.write_process(10)
            (self.proc / '10/status').write_text(status)
            with self.subTest(status=status), self.assertRaises(isolation.IsolationError):
                self.identity(10)

    def test_pid_reuse_during_snapshot_is_unknown(self):
        self.write_process(10, ticks='100')
        original = Path.read_text
        calls = 0
        def read(path, *args, **kwargs):
            nonlocal calls
            if path == self.proc / '10/stat':
                calls += 1
                if calls == 2:
                    self.write_process(10, ticks='101')
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', read), self.assertRaises(isolation.IsolationError):
            self.identity(10)

    def test_namespace_change_during_snapshot_is_unknown(self):
        self.write_process(10)
        original = os.readlink
        calls = 0
        def readlink(path, *args, **kwargs):
            nonlocal calls
            if Path(path) == self.proc / '10/ns/mnt':
                calls += 1
                if calls > 1:
                    return 'mnt:[99]'
            return original(path, *args, **kwargs)
        with mock.patch.object(os, 'readlink', readlink), self.assertRaises(isolation.IsolationError):
            self.identity(10)

    def test_permission_failure_is_not_foreign(self):
        self.write_process(10)
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == self.proc / '10/status':
                raise PermissionError('proc visibility denied')
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', read), self.assertRaises(PermissionError):
            self.identity(10)

    def test_observer_captures_matching_namespace_init_birth(self):
        self.write_process(os.getpid(), ticks='9007199254740993')
        identity = self.identity(os.getpid())
        self.assertEqual(identity['pid_ns_init_birth'], {'platform': 'linux', 'boot_id': BOOT, 'start_ticks': '1'})
        self.write_process(1, ppid=0, ns='pid:[99]', ticks='2')
        self.assertNotIn('pid_ns_init_birth', self.identity(os.getpid()))

    @unittest.skipUnless(sys.platform.startswith('linux') or sys.platform == 'darwin', 'Exact local identity requires Linux or Darwin')
    def test_real_local_birth_matches_existing_supervisor_convention(self):
        supervisor = SOURCE.parents[1] / 'scatter/npu_job_supervisor.py'
        spec = importlib.util.spec_from_file_location('supervisor_birth_under_test', supervisor)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        value = isolation.identify_process(os.getpid())
        self.assertEqual(value['birth'], module.process_birth(os.getpid()))
        self.assertEqual(value['birth'], isolation.process_birth(os.getpid()))
        self.assertEqual(value['ppid'], os.getppid())
        self.assertEqual(value['uid'], os.getuid())


class LineageTests(ProcFixture):
    def test_every_descendant_edge_and_unrelated_same_uid(self):
        root = self.tree()
        chain = isolation.verify_lineage(root, 30, proc_root=self.proc)
        self.assertEqual([row['pid'] for row in chain], [10, 20, 30])
        self.assertTrue(isolation.validate_lineage(chain, root))
        self.assertEqual(isolation.classify_process(self.identity(30), [chain]), 'own')
        self.assertIsNone(isolation.verify_lineage(root, 40, proc_root=self.proc))
        self.assertEqual(isolation.classify_process(self.identity(40), [chain]), 'foreign')

    def test_root_pid_reuse_and_missing_ancestor_are_unknown(self):
        root = self.tree()
        self.write_process(10, ticks='11')
        with self.assertRaises(isolation.IsolationError):
            isolation.verify_lineage(root, 30, proc_root=self.proc)
        self.write_process(10, ticks='10')
        (self.proc / '20/stat').unlink()
        with self.assertRaises(isolation.IsolationError):
            isolation.verify_lineage(root, 30, proc_root=self.proc)

    def test_intermediate_parent_reuse_is_rechecked(self):
        root = self.tree()
        original = isolation.identify_process
        calls = 0
        def identify(pid, **kwargs):
            nonlocal calls
            if pid == 30:
                calls += 1
                if calls == 2:
                    self.write_process(20, ppid=10, ticks='21')
            return original(pid, **kwargs)
        with mock.patch.object(isolation, 'identify_process', identify), self.assertRaises(isolation.IsolationError):
            isolation.verify_lineage(root, 30, proc_root=self.proc)

    def test_retained_lineage_survives_reparenting_but_not_birth_reuse(self):
        root = self.tree()
        chain = isolation.verify_lineage(root, 30, proc_root=self.proc)
        self.write_process(30, ppid=1, ticks='30')
        self.assertIsNone(isolation.verify_lineage(root, 30, proc_root=self.proc))
        self.assertEqual(isolation.classify_process(self.identity(30), [chain]), 'own')
        self.write_process(30, ppid=1, ticks='31')
        self.assertEqual(isolation.classify_process(self.identity(30), [chain]), 'foreign')
        self.assertEqual(isolation.classify_process(self.identity(30), []), 'unknown')

    def test_forged_or_incomplete_chain_cannot_prove_ownership(self):
        root = self.tree()
        chain = isolation.verify_lineage(root, 30, proc_root=self.proc)
        cases = [[], chain[1:], [chain[0], chain[-1]], chain + [chain[0]]]
        future = copy.deepcopy(chain); future[1]['birth']['start_ticks'] = '31'; cases.append(future)
        reused = copy.deepcopy(chain); reused[0]['birth']['start_ticks'] = '11'; cases.append(reused)
        for value in cases:
            with self.subTest(chain=value):
                self.assertFalse(isolation.validate_lineage(value, root))
        self.assertEqual(isolation.classify_process(None, [chain]), 'unknown')


class PidViewTests(ProcFixture):
    def test_proven_single_nspid_resolves_exact_birth(self):
        view, observer, driver = self.view()
        result = isolation.resolve_observed_pid(80, view, proc_root=self.proc,
                                                observer_context=observer, driver_identity=driver)
        self.assertEqual(result['birth']['start_ticks'], '80')
        self.assertEqual(result['nspid'], [80])
        self.assertIsNone(isolation.resolve_observed_pid(999, view, proc_root=self.proc,
                          observer_context=observer, driver_identity=driver))

    def test_no_proof_or_boolean_never_admits_single_nspid(self):
        view, observer, driver = self.view()
        for proof in (None, True, {'verified': True}, {**view, 'kind': 'unknown'}):
            with self.subTest(proof=proof), self.assertRaises(isolation.IsolationError):
                isolation.resolve_observed_pid(80, proof, proc_root=self.proc,
                    observer_context=observer, driver_identity=driver)

    def test_changed_scope_driver_or_namespace_init_rejects(self):
        view, observer, driver = self.view()
        changed = []
        for key, value in (('pid_ns', 'pid:[99]'), ('mnt_ns', 'mnt:[99]')):
            candidate = copy.deepcopy(observer); candidate[key] = value; changed.append(candidate)
        candidate = copy.deepcopy(observer); candidate['birth']['boot_id'] = 'a' * 36; changed.append(candidate)
        candidate = copy.deepcopy(observer); candidate['pid_ns_init_birth'] = observer['birth']; changed.append(candidate)
        for current in changed:
            with self.subTest(observer=current), self.assertRaises(isolation.IsolationError):
                isolation.validate_view_evidence(view, observer_context=current, driver_identity=driver)
        with self.assertRaises(isolation.IsolationError):
            isolation.validate_view_evidence(view, observer_context=observer, driver_identity={**driver, 'sha256': 'f' * 64})
        view['observer']['pid_ns_init_birth'] = {'platform': 'linux', 'boot_id': BOOT, 'start_ticks': '1'}
        current = copy.deepcopy(observer); current['pid_ns_init_birth']['start_ticks'] = '2'
        with self.assertRaises(isolation.IsolationError):
            isolation.validate_view_evidence(view, observer_context=current, driver_identity=driver)

    def test_probe_requires_exact_correlated_lifecycle(self):
        view, observer, driver = self.view()
        for key, value in (('before_pids', [80]), ('active_pids', []), ('after_pids', [80]),
                           ('exit_code', 1), ('exit_code', False), ('reported_pid', 81), ('evidence_sha256', 'bad')):
            proof = copy.deepcopy(view); proof['probe'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(isolation.IsolationError):
                isolation.validate_view_evidence(proof, observer_context=observer, driver_identity=driver)
        proof = copy.deepcopy(view); proof['probe']['active_process']['birth']['start_ticks'] = '81'
        with self.assertRaises(isolation.IsolationError):
            isolation.validate_view_evidence(proof, observer_context=observer, driver_identity=driver)

    def test_new_observer_process_in_same_proven_scope_needs_no_ttl(self):
        view, _, driver = self.view()
        self.write_process(71, ticks='9007199254740995')
        isolation.validate_view_evidence(view, observer_context=self.identity(71), driver_identity=driver)

    def test_namespace_translation_requires_declared_host_view(self):
        view, observer, driver = self.view()
        self.write_process(90, ns='pid:[99]', nspid=[90, 7], ticks='90')
        with self.assertRaises(isolation.IsolationError):
            isolation.resolve_observed_pid(90, view, proc_root=self.proc,
                observer_context=observer, driver_identity=driver)
        view['kind'] = 'host_proc'
        result = isolation.resolve_observed_pid(90, view, proc_root=self.proc,
            observer_context=observer, driver_identity=driver)
        self.assertEqual(result['pid'], 90)
        self.assertEqual(result['nspid'], [90, 7])
        # A numeric inner PID is not translated with a guessed constant offset.
        self.assertIsNone(isolation.resolve_observed_pid(7, view, proc_root=self.proc,
            observer_context=observer, driver_identity=driver))

    def test_driver_path_is_rehashed_when_no_current_identity_is_supplied(self):
        view, observer, driver = self.view()
        isolation.resolve_observed_pid(80, view, proc_root=self.proc, observer_context=observer)
        Path(driver['path']).write_bytes(b'changed driver')
        with self.assertRaises(isolation.IsolationError):
            isolation.resolve_observed_pid(80, view, proc_root=self.proc, observer_context=observer)


@unittest.skipUnless(sys.platform.startswith('linux') or sys.platform == 'darwin', 'POSIX flock lifecycle tests')
class LeaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kernelbridge-process-leases-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'shared'

    def acquire(self, key=DEVICE, root=None):
        lease = isolation.acquire_lease(key, root or self.root)
        self.addCleanup(lease.close)
        return lease

    def child(self, script, args=(), pass_fds=(), cwd=None):
        process = subprocess.Popen([sys.executable, '-B', '-c', script, str(SOURCE), *map(str, args)],
            cwd=cwd, pass_fds=pass_fds, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        self.addCleanup(cleanup)
        return process

    def ready_line(self, child):
        self.assertTrue(select.select([child.stdout], [], [], 10)[0], 'Local child did not report readiness')
        return child.stdout.readline().strip()

    def test_same_physical_device_across_deployments_uses_one_inode(self):
        first, second = self.base / 'deployment-a', self.base / 'deployment-b'
        first.mkdir(); second.mkdir()
        lease = self.acquire()
        path = Path(lease.evidence['root']) / lease.evidence['filename']
        inode = (path.stat().st_dev, path.stat().st_ino)
        script = '''import importlib.util,json,sys
s=importlib.util.spec_from_file_location('processes',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
try:
 l=m.acquire_lease(sys.argv[3],sys.argv[2]); print(json.dumps(l.evidence)); l.close()
except m.LeaseBusy: print('busy')
'''
        child = self.child(script, (self.root, DEVICE), cwd=second)
        out, err = child.communicate(timeout=10)
        self.assertEqual((child.returncode, out.strip(), err), (0, 'busy', ''))
        # Another physical chip is independently leasable, not a board-wide lock.
        other = self.acquire(DEVICE[:-1] + '0')
        self.assertNotEqual(other.evidence['ino'], lease.evidence['ino'])
        lease.close()
        child = self.child(script, (self.root, DEVICE), cwd=first)
        out, err = child.communicate(timeout=10)
        self.assertEqual((child.returncode, err), (0, ''))
        evidence = json.loads(out)
        self.assertEqual((evidence['dev'], evidence['ino']), inode)
        self.assertEqual(evidence['root'], str(self.root / str(os.getuid())))
        self.assertEqual(evidence['uid'], os.getuid())

    def test_inherited_descriptor_keeps_lock_until_last_holder_closes(self):
        lease = self.acquire()
        descriptor, evidence = lease.fd, lease.evidence
        script = '''import json,os,sys
fd=int(sys.argv[2]); st=os.fstat(fd); print(json.dumps([st.st_dev,st.st_ino]),flush=True)
sys.stdin.readline(); os.close(fd); print('closed',flush=True)
'''
        child = self.child(script, (descriptor,), pass_fds=(descriptor,))
        self.assertEqual(json.loads(self.ready_line(child)), [evidence['dev'], evidence['ino']])
        lease.close()
        with self.assertRaises(isolation.LeaseBusy):
            isolation.acquire_lease(DEVICE, self.root)
        child.stdin.write('release\n'); child.stdin.flush()
        self.assertEqual(self.ready_line(child), 'closed')
        child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0)
        replacement = self.acquire()
        self.assertEqual((replacement.evidence['dev'], replacement.evidence['ino']), (evidence['dev'], evidence['ino']))

    def test_detach_transfers_ownership_without_unlocking(self):
        lease = self.acquire()
        fd = lease.detach()
        try:
            lease.close()
            self.assertIsNone(lease.fd)
            with self.assertRaises(isolation.LeaseBusy):
                isolation.acquire_lease(DEVICE, self.root)
            with self.assertRaises(isolation.IsolationError):
                lease.detach()
        finally:
            os.close(fd)
        self.acquire()

    def test_persistent_file_is_never_truncated_or_unlinked(self):
        lease = self.acquire()
        path = Path(lease.evidence['root']) / lease.evidence['filename']
        os.write(lease.fd, b'original persistent lease metadata')
        inode = path.stat().st_ino
        lease.close()
        self.assertTrue(path.is_file())
        with self.acquire() as again:
            self.assertEqual(again.evidence['ino'], inode)
            self.assertEqual(path.read_bytes(), b'original persistent lease metadata')
        self.assertEqual(path.stat().st_ino, inode)

    def test_parent_alias_is_accepted_but_shared_root_symlink_is_rejected(self):
        alias = self.base / 'parent-alias'; alias.symlink_to(self.base, target_is_directory=True)
        lease = self.acquire(root=alias / 'shared')
        self.assertEqual(lease.evidence['shared_root'], str(self.root))
        linked = self.base / 'linked-root'; linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises((OSError, isolation.IsolationError)):
            isolation.acquire_lease(DEVICE, linked)

    def test_uid_directory_and_lock_symlinks_are_rejected(self):
        self.root.mkdir(mode=0o700)
        outside = self.base / 'outside'; outside.mkdir()
        scoped = self.root / str(os.getuid()); scoped.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, isolation.IsolationError)):
            isolation.acquire_lease(DEVICE, self.root)
        scoped.unlink(); scoped.mkdir(mode=0o700)
        target = outside / 'file'; target.write_bytes(b'unchanged')
        (scoped / (hashlib.sha256(DEVICE.encode()).hexdigest() + '.lock')).symlink_to(target)
        with self.assertRaises((OSError, isolation.IsolationError)):
            isolation.acquire_lease(DEVICE, self.root)
        self.assertEqual(target.read_bytes(), b'unchanged')

    def test_unsafe_directory_and_file_permissions_are_rejected(self):
        self.root.mkdir(); self.root.chmod(0o777)
        with self.assertRaises(isolation.IsolationError):
            isolation.acquire_lease(DEVICE, self.root)
        self.root.chmod(0o700)
        scoped = self.root / str(os.getuid()); scoped.mkdir(mode=0o700); scoped.chmod(0o770)
        with self.assertRaises(isolation.IsolationError):
            isolation.acquire_lease(DEVICE, self.root)
        scoped.chmod(0o700)
        path = scoped / (hashlib.sha256(DEVICE.encode()).hexdigest() + '.lock')
        path.write_bytes(b'original'); path.chmod(0o666)
        with self.assertRaises(isolation.IsolationError):
            isolation.acquire_lease(DEVICE, self.root)
        self.assertEqual(path.read_bytes(), b'original')

    def test_wrong_owner_and_declared_scope_fail_closed(self):
        lease = self.acquire(); lease.close()
        original = os.fstat
        def fstat(fd):
            info = original(fd)
            if info.st_ino == lease.evidence['ino']:
                fields = list(info); fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return info
        with mock.patch.object(os, 'fstat', fstat), self.assertRaises(isolation.IsolationError):
            isolation.acquire_lease(DEVICE, self.root)
        with self.assertRaises(isolation.IsolationError):
            isolation.acquire_lease(DEVICE, self.root, expected_scope={'uid': os.getuid() + 1})

    def test_replaced_inode_and_hardlinks_cannot_validate(self):
        lease = self.acquire()
        path = Path(lease.evidence['root']) / lease.evidence['filename']
        old = path.with_suffix('.old'); path.rename(old)
        path.write_bytes(b'new inode'); path.chmod(0o600)
        with self.assertRaises(isolation.IsolationError):
            isolation.validate_lease(lease.fd, lease.evidence, lease_root=self.root, device_key=DEVICE)
        path.unlink(); old.rename(path)
        os.link(path, path.with_suffix('.alias'))
        with self.assertRaises(isolation.IsolationError):
            isolation.validate_lease(lease.fd, lease.evidence, lease_root=self.root, device_key=DEVICE)

    def test_fdinfo_fixture_proves_existing_lock_without_reacquisition(self):
        # This is explicitly a Linux fdinfo parser fixture on every platform.
        lease = self.acquire()
        original_read = Path.read_text
        scope = {key: lease.evidence[key] for key in ('uid', 'mnt_ns', 'boot_id', 'platform')}
        expected = Path('/proc/self/fdinfo') / str(lease.fd)
        def read(path, *args, **kwargs):
            if path == expected:
                return 'pos:\t0\nlock:\t1: FLOCK  ADVISORY  WRITE 123 00:01:100 0 EOF\n'
            return original_read(path, *args, **kwargs)
        with mock.patch.object(isolation.sys, 'platform', 'linux'), mock.patch.object(isolation, '_scope', return_value=scope), \
                mock.patch.object(Path, 'read_text', read), mock.patch.object(isolation.fcntl, 'flock', side_effect=AssertionError('must not reacquire')):
            self.assertEqual(isolation.validate_lease(lease.fd, lease.evidence, lease_root=self.root, device_key=DEVICE), lease.evidence)
        def unlocked(path, *args, **kwargs):
            return 'pos:\t0\nflags:\t02000002\n' if path == expected else original_read(path, *args, **kwargs)
        with mock.patch.object(isolation.sys, 'platform', 'linux'), mock.patch.object(isolation, '_scope', return_value=scope), \
                mock.patch.object(Path, 'read_text', unlocked), mock.patch.object(isolation.fcntl, 'flock', side_effect=AssertionError('must not reacquire')), \
                self.assertRaises(isolation.IsolationError):
            isolation.validate_lease(lease.fd, lease.evidence, lease_root=self.root, device_key=DEVICE)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Real Linux fdinfo needs a Linux kernel; portable fixture tested separately')
    def test_real_linux_inherited_fdinfo_and_released_fd_rejection(self):
        lease = self.acquire()
        script = '''import importlib.util,json,sys
s=importlib.util.spec_from_file_location('processes',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
v=m.validate_lease(int(sys.argv[2]),json.loads(sys.argv[3]),lease_root=sys.argv[4],device_key=sys.argv[5]);print(json.dumps(v),flush=True)
'''
        child = self.child(script, (lease.fd, json.dumps(lease.evidence), self.root, DEVICE), pass_fds=(lease.fd,))
        out, err = child.communicate(timeout=10)
        self.assertEqual((child.returncode, err), (0, ''))
        self.assertEqual(json.loads(out), lease.evidence)
        isolation.fcntl.flock(lease.fd, isolation.fcntl.LOCK_UN)  # Only the test creates a broken-lock gap.
        with self.assertRaises(isolation.IsolationError):
            isolation.validate_lease(lease.fd, lease.evidence, lease_root=self.root, device_key=DEVICE)
        replacement = self.acquire()
        self.assertEqual(replacement.evidence['ino'], lease.evidence['ino'])

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin-specific inherited proof boundary')
    def test_darwin_inherited_fd_validation_is_unknown_not_faked(self):
        lease = self.acquire()
        with self.assertRaisesRegex(isolation.IsolationError, 'unavailable on this platform'):
            isolation.validate_lease(lease.fd, lease.evidence, lease_root=self.root, device_key=DEVICE)


if __name__ == '__main__':
    unittest.main()
