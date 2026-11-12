"""Real CPU child lifecycle tests. These are not NPU execution evidence."""
import base64
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples/scatter'))
import npu_job_supervisor as supervisor


@unittest.skipUnless(sys.platform.startswith('linux') or sys.platform == 'darwin', 'Exact process birth requires Linux or Darwin')
class NativeJobSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kb-supervisor-test-')
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'deployment'; self.root.mkdir()
        self.source = b'# CPU lifecycle fixture; never executed\n'
        self.request = {'remote_root': str(self.root), 'remote_attempt': str(self.root / 'attempts/one'),
                        'action_id': 'original-cpu-action', 'attempt': 1, 'mode': 'cpu_fixture', 'device': 0,
                        'source_sha256': hashlib.sha256(self.source).hexdigest(), 'source': base64.b64encode(self.source).decode(),
                        'supervisor_sha256': hashlib.sha256(Path(supervisor.__file__).read_bytes()).hexdigest()}
        self.folder = Path(self.request['remote_attempt'])
        self.handles = []

    def tearDown(self):
        # Only kill processes created by this test whose kernel birth still
        # matches the handle retained before any tampering scenario.
        for handle in self.handles:
            for pid, birth in ((handle['pid'], handle['process_birth']),
                               (handle['supervisor']['pid'], handle['supervisor']['birth'])):
                if supervisor.process_birth(pid, require_live=True) == birth:
                    os.kill(pid, signal.SIGKILL)
                self.wait(lambda: supervisor.process_birth(pid, require_live=True) != birth)
        self.temp.cleanup()

    def wait(self, function, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = function()
            if value:
                return value
            time.sleep(.01)
        self.fail('Original CPU fixture did not settle before deadline')

    def start(self, code='pass', request=None, argv=None):
        request = request or self.request
        argv = argv or [sys.executable, '-B', '-c', code]
        ack = supervisor.launch(request, argv)
        self.assertFalse(ack['existing'])
        if ack['handle'] is None:
            path = Path(request['remote_attempt']) / 'supervisor.log'
            self.fail('Supervisor did not acknowledge: ' + (path.read_text() if path.exists() else 'missing log'))
        self.handles.append(ack['handle'])
        return ack['handle'], argv

    def observe(self, handle=None, request=None):
        request = request or self.request
        expected = dict(request)
        if handle is not None:
            expected['handle'] = handle
        return supervisor.observe(request['remote_attempt'], expected=expected)

    def exited(self, handle, request=None):
        def terminal():
            value = self.observe(handle, request)
            return value if value['lifecycle_state'] == 'exited' else None
        value = self.wait(terminal)
        self.wait(lambda: not supervisor.process_birth(handle['supervisor']['pid'], require_live=True))
        return value

    def lock_available(self):
        with (self.root / '.npu-job.lock').open('a') as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(lease, fcntl.LOCK_UN)
            return True

    def test_real_zero_exit_without_benchmark_result_is_only_execution_evidence(self):
        handle, _ = self.start(argv=[shutil.which('true')])
        value = self.exited(handle)
        self.assertEqual(value['execution_exit']['returncode'], 0)
        self.assertIsNone(value['execution_exit']['signal'])
        self.assertFalse((self.folder / 'job/result.json').exists())
        self.assertNotIn('correct', value['execution_exit'])
        self.assertTrue(value['identity_verified'])
        raw = base64.b64decode(value['execution_exit_bytes'], validate=True)
        self.assertEqual(raw, (self.folder / 'execution-exit.json').read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), value['execution_exit_sha256'])
        self.assertEqual(supervisor.validate_exit_receipt(raw, handle=handle, intent=self.request), value['execution_exit'])

    def test_real_nonzero_exit_preserves_returncode_and_stderr(self):
        handle, _ = self.start('import sys; print("original cpu failure",file=sys.stderr); raise SystemExit(7)')
        value = self.exited(handle)
        self.assertEqual(value['execution_exit']['returncode'], 7)
        self.assertIsNone(value['execution_exit']['signal'])
        self.assertIn('original cpu failure', (self.folder / 'run.log').read_text())

    def test_real_signal_exit_has_negative_waitcode(self):
        handle, _ = self.start('import os,signal; os.kill(os.getpid(),signal.SIGTERM)')
        value = self.exited(handle)
        self.assertEqual(value['execution_exit']['returncode'], -signal.SIGTERM)
        self.assertEqual(value['execution_exit']['signal'], signal.SIGTERM)

    def test_lost_ack_recovers_original_bytes_and_repeat_never_popen(self):
        handle, argv = self.start('import time; time.sleep(.1)')
        value = self.exited(handle)
        original = {path.name: path.read_bytes() for path in self.folder.iterdir() if path.is_file()}
        launch_raw = base64.b64decode(value['original_launch_bytes'], validate=True)
        request_raw = base64.b64decode(value['execution_request_bytes'], validate=True)
        self.assertEqual(hashlib.sha256(launch_raw).hexdigest(), value['original_launch_sha256'])
        self.assertEqual(hashlib.sha256(request_raw).hexdigest(), value['execution_request_sha256'])
        self.assertEqual(supervisor.validate_launch_receipt(launch_raw, request_raw, intent=self.request), handle)
        with patch.object(supervisor.subprocess, 'Popen', side_effect=AssertionError('Never replace original job')):
            self.assertEqual(supervisor.launch(self.request, argv), {'existing': True, 'handle': handle})
        self.assertEqual({name: (self.folder / name).read_bytes() for name in original}, original)

    def test_existing_claim_without_execution_request_never_returns_unbound_handle(self):
        self.folder.mkdir(parents=True)
        (self.folder / 'candidate.py').write_bytes(self.source)
        (self.folder / 'intent.json').write_text(json.dumps({key: self.request[key] for key in supervisor.INTENT_KEYS}))
        (self.folder / 'launch.json').write_text(json.dumps({'pid': 99999999, 'attempt': str(self.folder)}))
        with patch.object(supervisor.subprocess, 'Popen', side_effect=AssertionError('Legacy must not relaunch')):
            self.assertEqual(supervisor.launch(self.request, ['/bin/true']), {'existing': True, 'handle': None})
        self.assertFalse((self.folder / 'execution-request.json').exists())
        self.assertFalse((self.folder / 'execution-exit.json').exists())

    def test_supervisor_loss_stays_unknown_and_child_keeps_device_lease(self):
        handle, argv = self.start(argv=['/bin/sleep', '30'])
        self.assertFalse(self.lock_available())
        owner = handle['supervisor']
        self.assertEqual(supervisor.process_birth(owner['pid']), owner['birth'])
        os.kill(owner['pid'], signal.SIGKILL)
        self.wait(lambda: not supervisor.process_birth(owner['pid'], require_live=True))
        value = self.observe(handle)
        self.assertEqual(value['lifecycle_state'], 'unknown'); self.assertTrue(value['alive'])
        self.assertNotIn('execution_exit', value)
        self.assertFalse(self.lock_available())
        with patch.object(supervisor.subprocess, 'Popen', side_effect=AssertionError('No replacement after lost supervisor')):
            self.assertEqual(supervisor.launch(self.request, argv)['handle'], handle)
        self.assertEqual(supervisor.process_birth(handle['pid']), handle['process_birth'])
        os.kill(handle['pid'], signal.SIGTERM)
        self.wait(lambda: not supervisor.process_birth(handle['pid'], require_live=True))
        self.wait(self.lock_available)
        self.assertEqual(self.observe(handle)['lifecycle_state'], 'unknown')
        self.assertFalse((self.folder / 'execution-exit.json').exists())

    def test_changed_pid_birth_and_changed_ack_cannot_report_running_original_job(self):
        handle, _ = self.start(argv=['/bin/sleep', '30'])
        raw = (self.folder / 'launch.json').read_bytes()
        changed = copy.deepcopy(handle)
        field = 'start_ticks' if sys.platform.startswith('linux') else 'start_seconds'
        changed['process_birth'][field] = str(int(changed['process_birth'][field]) + 1)
        (self.folder / 'launch.json').write_text(json.dumps(changed))
        try:
            with self.assertRaisesRegex(ValueError, 'handle changed'):
                self.observe(handle)
        finally:
            (self.folder / 'launch.json').write_bytes(raw)
        original = supervisor.process_birth
        with patch.object(supervisor, 'process_birth', side_effect=lambda pid, **kw: changed['process_birth'] if pid == handle['pid'] else original(pid, **kw)):
            value = self.observe(handle)
        self.assertEqual(value['lifecycle_state'], 'unknown'); self.assertFalse(value['alive'])

    def test_historical_exit_is_valid_even_after_pid_reuse(self):
        handle, _ = self.start()
        value = self.exited(handle)
        with patch.object(supervisor, 'process_birth', side_effect=AssertionError('Historical exit cannot use current PID identity')):
            self.assertEqual(self.observe(handle), value)

    def test_bound_root_alias_allowed_but_alternate_alias_and_inner_links_rejected(self):
        alias = self.base / 'declared-root'; alias.symlink_to(self.root, target_is_directory=True)
        request = dict(self.request, remote_root=str(alias), remote_attempt=str(alias / 'attempts/one'))
        handle, argv = self.start(request=request)
        self.exited(handle, request)
        self.assertEqual(supervisor.launch(request, argv)['handle'], handle)
        other = self.base / 'other-root'; other.symlink_to(self.root, target_is_directory=True)
        changed = dict(request, remote_root=str(other), remote_attempt=str(other / 'attempts/one'))
        with self.assertRaisesRegex(ValueError, 'deployment binding changed'):
            supervisor.launch(changed, argv)
        inner = self.root / 'inner'; inner.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'folder differs'):
            supervisor.observe(inner, expected=request)
        with self.assertRaisesRegex(ValueError, 'internal link'):
            supervisor.launch(dict(self.request, remote_attempt=str(inner)), argv)

    def test_internal_evidence_links_are_rejected_even_with_identical_bytes(self):
        handle, _ = self.start(); self.exited(handle)
        for name in ('candidate.py', 'intent.json', 'execution-request.json', 'launch.json', 'supervisor.json', 'execution-exit.json'):
            path = self.folder / name; raw = path.read_bytes()
            for location in (self.folder, self.base):
                with self.subTest(name=name, target=location):
                    target = location / ('copy-' + name); target.write_bytes(raw)
                    path.unlink(); path.symlink_to(target)
                    try:
                        with self.assertRaises(ValueError):
                            self.observe(handle)
                    finally:
                        path.unlink(); path.write_bytes(raw); target.unlink()

    def test_pure_raw_receipt_validators_reject_identity_and_exit_tampering(self):
        handle, _ = self.start(); value = self.exited(handle)
        launch_raw = base64.b64decode(value['original_launch_bytes']); request_raw = base64.b64decode(value['execution_request_bytes'])
        request = json.loads(request_raw)
        for key, replacement in (('action_id', 'other'), ('attempt', 2), ('source_sha256', '0' * 64), ('mode', 'other'), ('device', 1)):
            changed = copy.deepcopy(request); changed['intent'][key] = replacement
            with self.subTest(request=key), self.assertRaises(ValueError):
                supervisor.validate_launch_receipt(launch_raw, json.dumps(changed).encode(), intent=self.request)
        for key, replacement in (('execution_request_sha256', '0' * 64), ('argv', ['/bin/false']), ('attempt', '/other'), ('supervisor', {})):
            changed = copy.deepcopy(handle); changed[key] = replacement
            with self.subTest(handle=key), self.assertRaises(ValueError):
                supervisor.validate_launch_receipt(json.dumps(changed).encode(), request_raw, intent=self.request)
        record = value['execution_exit']
        for key, replacement in (('schema_version', 'wrong'), ('supervisor', {}), ('process_birth', {}), ('returncode', True),
                                 ('signal', 9), ('finished_ns', 1.5), ('execution_request_sha256', '0' * 64), ('handle', {})):
            changed = copy.deepcopy(record); changed[key] = replacement
            with self.subTest(exit=key), self.assertRaises((ValueError, KeyError)):
                supervisor.validate_exit_receipt(json.dumps(changed).encode(), handle=handle, intent=self.request)

    def test_same_action_changed_source_and_module_pin_rejected_before_dispatch(self):
        handle, argv = self.start(); self.exited(handle)
        with patch.object(supervisor.subprocess, 'Popen', side_effect=AssertionError('No dispatch for mutated request')):
            with self.assertRaisesRegex(ValueError, 'different input'):
                supervisor.launch(dict(self.request, action_id='other'), argv)
            with self.assertRaisesRegex(ValueError, 'candidate hash'):
                supervisor.launch(dict(self.request, source=base64.b64encode(b'changed').decode()), argv)
            with self.assertRaisesRegex(ValueError, 'supervisor code'):
                supervisor.launch(dict(self.request, supervisor_sha256='0' * 64), argv)


if __name__ == '__main__':
    unittest.main()
