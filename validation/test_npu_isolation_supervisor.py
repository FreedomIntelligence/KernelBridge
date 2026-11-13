"""Local CPU lifecycle and entry checks; no device or framework invocation."""
import base64
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_npu_job_supervisor as fixture

supervisor = fixture.supervisor
ROOT = Path(__file__).resolve().parents[1]


class IsolationSupervisorTests(unittest.TestCase):
    setUp = fixture.NativeJobSupervisorTests.setUp
    tearDown = fixture.NativeJobSupervisorTests.tearDown
    wait = fixture.NativeJobSupervisorTests.wait
    start = fixture.NativeJobSupervisorTests.start
    observe = fixture.NativeJobSupervisorTests.observe
    exited = fixture.NativeJobSupervisorTests.exited

    def guarded_request(self):
        from npu_isolation import normalize_policy
        request = dict(self.request, isolation_policy=normalize_policy({'logical_device': 0,
            'lease_root': str(self.base / 'shared-leases')}))
        for key, name in (('isolation_code_sha256', 'examples/scatter/npu_isolation.py'),
                          ('process_isolation_code_sha256', 'examples/operator_pipeline/process_isolation.py'),
                          ('npu_enter_code_sha256', 'examples/scatter/npu_enter.py')):
            request[key] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        return request

    def test_unknown_view_never_forks_native_child_and_lost_ack_recovers_admission(self):
        request = self.guarded_request()
        marker = self.root / 'must-not-run'
        argv = [sys.executable, '-B', '-c', 'from pathlib import Path; Path(' + repr(str(marker)) + ').touch()']
        first = supervisor.launch(request, argv)
        self.assertEqual(first['status'], 'not_started', first)
        self.assertIsNone(first['handle']); self.assertFalse(marker.exists())
        self.assertFalse((self.folder / 'launch.json').exists())
        self.assertFalse((self.folder / 'execution-exit.json').exists())
        raw = base64.b64decode(first['admission_bytes'], validate=True)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), first['admission_sha256'])
        self.assertEqual(json.loads(raw)['reason_code'], 'observation_unknown')
        bound = json.loads(base64.b64decode(first['execution_request_bytes']))
        self.assertEqual(bound['claim_lock']['kind'], 'original_attempt')
        self.assertTrue(Path(bound['claim_lock']['path']).is_file())
        self.assertFalse((self.root / '.npu-job.lock').exists())
        with patch.object(supervisor.subprocess, 'Popen', side_effect=AssertionError('Original admission must not launch again')):
            repeated = supervisor.launch(request, argv)
        self.assertTrue(repeated['existing'])
        self.assertEqual(first['admission_bytes'], repeated['admission_bytes'])
        self.assertFalse(marker.exists())

    def test_same_attempt_cannot_borrow_admission_under_changed_policy(self):
        request = self.guarded_request(); argv = [sys.executable, '-B', '-c', 'raise AssertionError("never run")']
        self.assertEqual(supervisor.launch(request, argv)['status'], 'not_started')
        changed = dict(request, isolation_policy=dict(request['isolation_policy'], sample_interval_ms=500))
        with self.assertRaisesRegex(ValueError, 'isolation_policy'):
            supervisor.launch(changed, argv)

    def test_unreadable_occupancy_proof_does_not_hide_real_child_exit(self):
        handle, _ = self.start('raise SystemExit(7)')
        original = self.exited(handle)
        for error in (OSError('read unavailable'), ValueError('Linked isolation evidence'),
                      ValueError('Isolation evidence exceeds retention bound')):
            with self.subTest(error=str(error)), patch.object(supervisor, '_isolation_module', return_value=SimpleNamespace(
                    read_evidence=lambda folder, problem=error: (_ for _ in ()).throw(problem))):
                observed = self.observe(handle)
            self.assertEqual(observed['lifecycle_state'], 'exited')
            self.assertEqual(observed['execution_exit_bytes'], original['execution_exit_bytes'])
            self.assertEqual(observed['execution_exit']['returncode'], 7)
            self.assertIn(str(error), observed['isolation_evidence_error'])

    def test_unreadable_supervisor_birth_does_not_hide_original_terminal_exit(self):
        handle, _ = self.start('raise SystemExit(7)')
        original = self.exited(handle)
        with patch.object(supervisor, '_isolation_module', return_value=SimpleNamespace(read_evidence=lambda _: {})), \
                patch.object(supervisor, 'process_birth', side_effect=OSError('Original process view unavailable')):
            observed = self.observe(handle)
        self.assertEqual(observed['lifecycle_state'], 'exited')
        self.assertEqual(observed['execution_exit_bytes'], original['execution_exit_bytes'])
        self.assertIsNone(observed['supervisor_alive'])
        self.assertIn('process view unavailable', observed['supervisor_observation_error'])

    def test_pinned_observer_executes_original_bytes_instead_of_stale_bytecode(self):
        folder = self.root / 'observer-code'; folder.mkdir()
        source = folder / 'npu_isolation.py'
        source.write_text('value = "old"\n')
        before = source.stat()
        py_compile.compile(str(source), doraise=True)
        source.write_text('value = "new"\n')
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        request = {'isolation_policy': {}}
        for key, name in (('isolation_code_sha256', 'npu_isolation.py'),
                          ('process_isolation_code_sha256', 'process_isolation.py'),
                          ('npu_enter_code_sha256', 'npu_enter.py')):
            path = folder / name
            if not path.exists(): path.write_text('# pinned dependency\n')
            request[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        with patch.object(supervisor, '__file__', str(folder / 'npu_job_supervisor.py')):
            module = supervisor._isolation_module(request)
        self.assertEqual(module.value, 'new')

    def test_no_start_requires_readable_original_proof(self):
        request = self.guarded_request()
        self.assertEqual(supervisor.launch(request, [sys.executable, '-B', '-c', 'pass'])['status'], 'not_started')
        with patch.object(supervisor, '_isolation_module', return_value=SimpleNamespace(
                read_evidence=lambda folder: (_ for _ in ()).throw(ValueError('Missing proof')))):
            with self.assertRaisesRegex(ValueError, 'lacks readable isolation proof'):
                supervisor.observe(self.folder, expected=request)

    def test_exit_before_final_proof_reports_real_supervisor_still_settling(self):
        copied = self.root / 'tools/npu_job_supervisor.py'; copied.parent.mkdir()
        source = Path(supervisor.__file__).read_text()
        needle = "        _save(folder / 'execution-exit.json', record)\n"
        self.assertEqual(source.count(needle), 1)
        copied.write_text(source.replace(needle, needle +
            "        (folder / 'exit-ready').touch()\n"
            "        while not (folder / 'release-final-proof').exists(): time.sleep(.01)\n"))
        request = dict(self.request, supervisor_sha256=hashlib.sha256(copied.read_bytes()).hexdigest())
        with patch.object(supervisor, '__file__', str(copied)):
            handle, _ = self.start(request=request)
            self.wait(lambda: (self.folder / 'exit-ready').exists())
            with patch.object(supervisor, '_isolation_module', return_value=SimpleNamespace(read_evidence=lambda folder: {})):
                observed = self.observe(handle, request)
            self.assertEqual(observed['lifecycle_state'], 'exited')
            self.assertTrue(observed['supervisor_alive']); self.assertFalse(observed['alive'])
            self.assertEqual(observed['execution_exit']['returncode'], 0)
            self.assertNotIn('isolation_receipt_bytes', observed)
            (self.folder / 'release-final-proof').touch()
            self.exited(handle, request)


class IsolatedEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.sdk = self.root / 'sdk'; self.sdk.mkdir()
        self.entry = ROOT / 'examples/scatter/npu_enter.py'
        self.profile = self.root / 'profile.json'
        self.profile.write_text(json.dumps({'sdk': str(self.sdk), 'python': sys.executable}))
        self.marker = self.root / 'benchmark-executed'
        self.child = self.root / 'cpu.py'
        self.child.write_text('from pathlib import Path\nPath(' + repr(str(self.marker)) + ').touch()\n')

    def enter(self, *, guarded=True, initial=None, activated=None, wrong_hash=False):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        for key in ('ASCEND_VISIBLE_DEVICES', 'ASCEND_RT_VISIBLE_DEVICES',
                    'KERNELBRIDGE_ISOLATION_REQUEST_PATH', 'KERNELBRIDGE_ISOLATION_REQUEST_SHA256'):
            env.pop(key, None)
        env.update(initial or {})
        commands = ['export ASCEND_HOME_PATH=' + shlex.quote(str(self.sdk))]
        commands.extend('export ' + name + '=' + shlex.quote(value) for name, value in (activated or {}).items())
        (self.sdk / 'set_env.sh').write_text('\n'.join(commands) + '\n')
        if guarded:
            request = {'isolation_policy': {'required': True, 'logical_device': 0}, 'intent': {'device': 0},
                       'npu_enter_code_sha256': hashlib.sha256(self.entry.read_bytes()).hexdigest()}
            raw = json.dumps(request).encode(); path = self.root / 'execution-request.json'; path.write_bytes(raw)
            env.update(KERNELBRIDGE_ISOLATION_REQUEST_PATH=str(path),
                       KERNELBRIDGE_ISOLATION_REQUEST_SHA256='0' * 64 if wrong_hash else hashlib.sha256(raw).hexdigest())
        return subprocess.run([sys.executable, '-B', str(self.entry), str(self.profile), str(self.child)],
                              env=env, capture_output=True, timeout=10)

    def test_empty_visibility_allows_exact_guarded_entry(self):
        result = self.enter()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self.marker.exists())

    def test_runtime_remap_is_rejected_both_before_and_after_sdk_activation(self):
        for name in ('ASCEND_VISIBLE_DEVICES', 'ASCEND_RT_VISIBLE_DEVICES'):
            for phase in ('initial', 'activated'):
                with self.subTest(name=name, phase=phase):
                    result = self.enter(**{phase: {name: '0,1'}})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b'does not support a visibility remap', result.stderr)
                    self.assertFalse(self.marker.exists())

    def test_request_hash_mismatch_never_executes_benchmark(self):
        result = self.enter(wrong_hash=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'entry request changed', result.stderr)
        self.assertFalse(self.marker.exists())

    def test_legacy_entry_keeps_existing_visibility_behavior(self):
        result = self.enter(guarded=False, activated={'ASCEND_VISIBLE_DEVICES': '0,1'})
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(self.marker.exists())


if __name__ == '__main__': unittest.main()
