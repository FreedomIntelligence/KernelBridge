"""Real CPU child lifecycle through native launch/collect, with no NPU imports."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples/scatter'))
import npu_job_supervisor as supervisor
import npu_trial
from ascend_sources import candidate


@unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'Exact process birth requires Darwin or Linux')
class SupervisedTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.deployment = self.root / 'deployment'; (self.deployment / 'tools').mkdir(parents=True)
        for name in ('npu_job_supervisor.py', 'npu_enter.py'):
            shutil.copyfile(Path(npu_trial.__file__).with_name(name), self.deployment / 'tools' / name)
        sdk = self.deployment / 'sdk'; sdk.mkdir()
        (sdk / 'set_env.sh').write_text('export ASCEND_HOME_PATH="' + str(sdk) + '"\n')
        (self.deployment / 'profile.json').write_text(json.dumps({'sdk': str(sdk), 'python': sys.executable}))
        self.source = self.root / 'candidate.py'; self.source.write_text(candidate('max', 33))
        self.state = self.root / 'state'
        self.config = {'candidate': str(self.source), 'state': str(self.state), 'mode': 'max',
                       'remote_root': str(self.deployment), 'attempt_namespace': 'cpu-fixture',
                       'wait_seconds': 5, 'poll_seconds': .02}
        self.action = {'id': 'original-action', 'arguments': {'hypothesis': 'CPU lifecycle fixture only'}}
        self.calls = []; self.handles = []
        self.addCleanup(self.cleanup_children)
        self.benchmark('success')

    def cleanup_children(self):
        for handle in self.handles:
            for pid, birth in ((handle['pid'], handle['process_birth']),
                               (handle['supervisor']['pid'], handle['supervisor']['birth'])):
                if supervisor.process_birth(pid, require_live=True) == birth:
                    os.kill(pid, signal.SIGKILL)

    def benchmark(self, behavior):
        # Exercise the actual entry exec and supervisor, but never import a
        # candidate, framework, compiler, device runtime, or execute an operator.
        code = '''import argparse,hashlib,json,os,signal,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--candidate');p.add_argument('--output');a,_=p.parse_known_args()
out=Path(a.output);out.mkdir();time.sleep(.05)
print('[ERROR] CPU fixture diagnostic retained',flush=True)
mode=BEHAVIOR
if mode=='signal':os.kill(os.getpid(),signal.SIGTERM)
if mode=='exit7':raise SystemExit(7)
if mode in ('success','early-result'):
 (out/'result.json').write_text(json.dumps({'status':'completed','correct':True,'source_sha256':hashlib.sha256(Path(a.candidate).read_bytes()).hexdigest(),'cases':[]}))
if mode in ('sleep','early-result'):time.sleep(30)
'''.replace('BEHAVIOR', repr(behavior))
        (self.deployment / 'tools/npu_benchmark.py').write_text(code)

    def transport(self, argv, stdin=b'', timeout=45):
        self.calls.append(argv[3])
        result = subprocess.run([sys.executable, *argv[1:]], input=stdin, capture_output=True, timeout=timeout)
        if argv[3] == npu_trial.LAUNCH and result.returncode == 0:
            handle = json.loads(result.stdout).get('handle')
            if handle is not None: self.handles.append(handle)
        return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}

    def run_trial(self, transport=None):
        with patch.object(npu_trial, 'transport', return_value=transport or self.transport):
            return npu_trial.trial(self.config, self.action)

    @property
    def folder(self): return self.state / 'attempt-0001'

    def test_real_entry_exec_preserves_child_birth_and_result_requires_verified_exit(self):
        result = self.run_trial()
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(result['data']['correct']); self.assertEqual(result['data']['result_origin'], 'benchmark')
        exit_record = json.loads((self.folder / 'execution-exit.json').read_bytes())
        self.assertEqual(exit_record['returncode'], 0)
        self.assertEqual(exit_record['handle'], self.handles[0])
        self.assertEqual(result['data']['run_log']['status'], 'completed')
        self.assertIn(b'CPU fixture diagnostic', (self.folder / 'run.log').read_bytes())
        self.assertEqual(self.calls.count(npu_trial.LAUNCH), 1)

    def test_exit_zero_without_result_is_nonqualifying_not_fabricated_benchmark(self):
        self.benchmark('no-result')
        result = self.run_trial()
        self.assertEqual(result['status'], 'completed', result)
        self.assertFalse(result['data']['correct']); self.assertTrue(result['data']['nonqualifying'])
        self.assertEqual(result['data']['returncode'], 0)
        self.assertEqual(result['data']['result_origin'], 'supervisor_exit')
        self.assertFalse((self.folder / 'device-result.json').exists())
        self.assertNotIn('speedup', result['data']); self.assertNotIn('cases', result['data'])
        self.assertEqual(result['data']['run_log']['status'], 'completed')
        sealed = (self.folder / 'result.json').read_bytes()
        log = npu_trial.recover_run_log(self.folder, self.transport, remote_root=self.config['remote_root'])
        self.assertEqual(log['status'], 'completed', log)
        self.assertEqual((self.folder / 'result.json').read_bytes(), sealed)
        self.assertEqual(self.calls.count(npu_trial.LAUNCH), 1)

    def test_actual_signal_is_returned_with_original_exit_bytes(self):
        self.benchmark('signal')
        result = self.run_trial()
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['data']['returncode'], -signal.SIGTERM)
        self.assertEqual(result['data']['signal'], signal.SIGTERM)
        self.assertFalse(result['data']['correct'])
        self.assertFalse((self.folder / 'device-result.json').exists())
        raw = (self.folder / 'execution-exit.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result['data']['execution_exit_sha256'])

    def test_lost_ack_keeps_raw_remote_proof_and_never_launches_again(self):
        self.benchmark('sleep'); self.config['wait_seconds'] = 0
        def lost_ack(argv, **kwargs):
            reply = self.transport(argv, **kwargs)
            if argv[3] == npu_trial.LAUNCH: raise TimeoutError('Discard only original launch acknowledgement')
            return reply
        first = self.run_trial(lost_ack)
        self.assertEqual(first['status'], 'unknown', first)
        self.assertTrue(first['data']['identity_verified']); self.assertEqual(first['data']['original_handle'], self.handles[0])
        self.assertFalse((self.folder / 'launch.json').exists())
        self.assertEqual(first['data']['original_launch'], str(self.folder / 'observed-launch.json'))
        launch_raw = (self.folder / 'observed-launch.json').read_bytes()
        request_raw = (self.folder / 'observed-execution-request.json').read_bytes()
        intent = json.loads((self.folder / 'intent.json').read_bytes())
        self.assertEqual(supervisor.validate_launch_receipt(launch_raw, request_raw, intent=intent), self.handles[0])
        self.assertEqual(self.run_trial()['status'], 'unknown')
        self.assertEqual(self.calls.count(npu_trial.LAUNCH), 1)
        self.assertFalse((self.folder / 'result.json').exists())

    def test_written_result_before_process_exit_stays_unknown_and_lease_stays_held(self):
        self.benchmark('early-result'); self.config['wait_seconds'] = 0
        first = self.run_trial(); handle = self.handles[0]
        output = Path(handle['attempt']) / 'job/result.json'
        deadline = time.monotonic() + 3
        while not output.exists() and time.monotonic() < deadline: time.sleep(.02)
        self.assertTrue(output.exists())
        result = self.run_trial()
        self.assertEqual(result['status'], 'unknown', result)
        self.assertEqual(first['data']['original_handle'], result['data']['original_handle'])
        self.assertFalse((self.folder / 'result.json').exists())
        import fcntl
        with (self.deployment / '.npu-job.lock').open('a') as stream:
            with self.assertRaises(BlockingIOError): fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_bare_identity_boolean_cannot_recover_a_lost_ack(self):
        self.benchmark('sleep'); self.config['wait_seconds'] = 0
        def incomplete(argv, **kwargs):
            reply = self.transport(argv, **kwargs)
            if argv[3] == npu_trial.LAUNCH: raise TimeoutError('Lost ACK')
            if argv[3] == npu_trial.COLLECT:
                value = json.loads(reply['stdout']); value.pop('original_launch_bytes', None)
                reply['stdout'] = json.dumps(value).encode()
            return reply
        result = self.run_trial(incomplete)
        self.assertEqual(result['status'], 'unknown', result)
        self.assertFalse(result['data']['identity_verified']); self.assertIsNone(result['data']['original_handle'])
        self.assertFalse((self.folder / 'observed-launch.json').exists())
        self.assertFalse((self.folder / 'result.json').exists())

    def test_observer_failure_returns_only_the_original_ack_handle(self):
        self.benchmark('sleep'); self.config['wait_seconds'] = 0
        def unavailable(argv, **kwargs):
            if argv[3] == npu_trial.COLLECT: raise TimeoutError('Read transport failed')
            return self.transport(argv, **kwargs)
        result = self.run_trial(unavailable)
        self.assertEqual(result['status'], 'unknown', result)
        self.assertTrue(result['data']['identity_verified']); self.assertEqual(result['data']['original_handle'], self.handles[0])
        self.assertEqual(result['data']['original_launch'], str(self.folder / 'launch.json'))
        self.assertFalse((self.folder / 'result.json').exists())

    def test_deployment_alias_is_allowed_but_internal_log_link_is_rejected(self):
        alias = self.root / 'root-alias'; alias.symlink_to(self.deployment, target_is_directory=True)
        self.config['remote_root'] = str(alias)
        result = self.run_trial()
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['data']['run_log']['status'], 'completed', result)
        original_log = self.folder / 'run.log'; sealed = original_log.read_bytes()
        remote = Path(self.handles[0]['attempt']).resolve(); outside = self.root / 'outside.log'
        outside.write_bytes((remote / 'run.log').read_bytes()); (remote / 'run.log').unlink()
        (remote / 'run.log').symlink_to(outside)
        record = npu_trial.recover_run_log(self.folder, self.transport, remote_root=str(alias))
        self.assertIn('link', record['recovery_error'])
        self.assertEqual(original_log.read_bytes(), sealed)
        self.assertEqual(self.calls.count(npu_trial.LAUNCH), 1)


if __name__ == '__main__': unittest.main()
