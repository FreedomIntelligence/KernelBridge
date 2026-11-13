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
import npu_pipeline_driver as adapter
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
        (self.deployment / 'tools/npu_eager.py').write_text('# CPU fixture oracle; never executed\n')
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
if mode in ('success','early-result','failed-report','success-exit7'):
 failed=mode=='failed-report'
 report={'status':'failed' if failed else 'completed','correct':not failed,'source_sha256':hashlib.sha256(Path(a.candidate).read_bytes()).hexdigest(),'cases':[],
         'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'baseline_sha256':hashlib.sha256(Path(__file__).with_name('npu_eager.py').read_bytes()).hexdigest(),
         'device':0,'warmup':3,'repeats':10,'pairs':3,'build_completed':not failed,'failure_stage':'compile' if failed else None,
         'message':'fixture.cpp:7: error: original undeclared SDK symbol' if failed else ''}
 (out/'result.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\\n')
 if failed:raise SystemExit(1)
 if mode=='success-exit7':raise SystemExit(7)
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

    def projection_request(self):
        files = {name: adapter.sha(self.deployment / 'tools' / name)
                 for name in ('npu_benchmark.py', 'npu_eager.py')}
        oracle = {'root': str(self.deployment / 'tools'), 'files': files, 'sha256': adapter.manifest_sha(files)}
        source_files = {'candidate.py': adapter.sha(self.folder / 'candidate.py')}
        source = {'root': str(self.folder), 'files': source_files, 'sha256': adapter.manifest_sha(source_files)}
        contract = {'task': {'outputs': 'CPU fixture only'}, 'suite': [{'id': 'cpu-case'}],
                    'oracle': {'kind': 'independent', 'description': 'CPU fixture', 'files': oracle},
                    'measurement': {'device': 0, 'warmup': 3, 'repeats': 10, 'pairs': 3}}
        frozen = json.loads(json.dumps(contract))
        frozen['oracle']['files'] = {name: str(self.deployment / 'tools' / name) for name in files}
        config = dict(self.config, contract=frozen)
        request = {'source': source, 'binding': {'source': source['sha256']}, 'contract': contract,
                   'handle': self.handles[-1], 'role': 'F', 'action_id': 'cpu-original-projection',
                   'output': str(self.root / ('projection-' + self.config['mode']))}
        return config, request

    def test_failed_report_then_exit_one_keeps_hash_and_compiler_feedback_for_all_modes(self):
        self.benchmark('failed-report')
        for mode in ('max', 'min', 'matmul'):
            with self.subTest(mode=mode):
                self.state = self.root / ('state-' + mode)
                self.config.update(mode=mode, state=str(self.state))
                outcome = self.run_trial()
                self.assertEqual(outcome['status'], 'completed', outcome)
                self.assertEqual(outcome['data']['result_origin'], 'benchmark')
                observation = json.loads((self.folder / 'observation.json').read_bytes())
                self.assertEqual(observation['execution_exit']['returncode'], 1)
                expected = observation['result_sha256']
                self.assertEqual(outcome['data']['remote_result_sha256'], expected)
                self.assertEqual(outcome['data']['run_log']['remote_result_sha256'], expected)
                self.assertEqual(adapter.sha(self.folder / 'device-result.json'), expected)
                self.assertFalse((self.folder / 'execution-failure.json').exists())
                config, request = self.projection_request()
                folder, result, summary, evidence = adapter.original(config, request)
                feedback = adapter.terminal_feedback(folder, request, result, evidence)
                self.assertEqual(feedback['failure_stage'], 'compile')
                self.assertIn('undeclared SDK symbol', feedback['first_compiler_diagnostic']['text'])
                self.assertEqual(feedback['remote_result_sha256'], expected)
                self.assertEqual(feedback['run_log']['artifact']['sha256'], adapter.sha(folder / 'run.log'))
                checks = adapter.execute(config, 'check', request)
                self.assertEqual(checks['checks'], dict.fromkeys(('build', 'semantics', 'full'), False))

    def test_forged_supervised_report_hash_cannot_become_a_terminal_result(self):
        self.benchmark('failed-report')
        def forged(argv, **kwargs):
            reply = self.transport(argv, **kwargs)
            if argv[3] == npu_trial.COLLECT:
                value = json.loads(reply['stdout']); value['result_sha256'] = '0' * 64
                reply['stdout'] = json.dumps(value).encode()
            return reply
        outcome = self.run_trial(forged)
        self.assertEqual(outcome['status'], 'unknown', outcome)
        self.assertIn('hash changed', outcome['data']['error'])
        self.assertFalse((self.folder / 'result.json').exists())
        self.assertFalse((self.folder / 'device-result.json').exists())
        self.assertEqual(self.calls.count(npu_trial.LAUNCH), 1)

    def test_nonzero_exit_with_success_report_is_unknown_and_adapter_rejects_it(self):
        self.benchmark('success-exit7')
        outcome = self.run_trial()
        self.assertEqual(outcome['status'], 'unknown', outcome)
        self.assertIn('Nonzero native exit', outcome['data']['error'])
        self.assertFalse((self.folder / 'result.json').exists())
        self.assertFalse((self.folder / 'device-result.json').exists())
        observation = json.loads((self.folder / 'observation.json').read_bytes())
        # Reproduce a conflicting pre-existing projection in this private CPU
        # fixture; the independent adapter must not upgrade it to F success.
        npu_trial.save(self.folder / 'device-result.json', observation['result'])
        npu_trial.save(self.folder / 'result.json', {'status': 'completed', 'correct': True,
            'result_origin': 'benchmark', 'remote_result_sha256': observation['result_sha256'],
            'remote_attempt': self.handles[0]['attempt'], 'original_handle': self.handles[0],
            'execution_exit_sha256': observation['execution_exit_sha256'],
            'execution_exit_path': str(self.folder / 'execution-exit.json')})
        config, request = self.projection_request()
        with self.assertRaisesRegex(ValueError, 'Nonzero native exit'):
            adapter.execute(config, 'check', request)
        self.assertEqual(self.calls.count(npu_trial.LAUNCH), 1)

    def test_failed_report_cannot_borrow_a_changed_exit_or_summary_binding(self):
        self.benchmark('failed-report')
        self.assertEqual(self.run_trial()['status'], 'completed')
        config, request = self.projection_request()
        exit_path = self.folder / 'execution-exit.json'; raw_exit = exit_path.read_bytes()
        summary_path = self.folder / 'result.json'; raw_summary = summary_path.read_bytes()
        try:
            changed = json.loads(raw_exit); changed['returncode'] = 2
            exit_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, 'supervisor exit binding changed'):
                adapter.execute(config, 'check', request)
            exit_path.write_bytes(raw_exit)
            changed = json.loads(raw_summary); changed['execution_exit_sha256'] = '0' * 64
            npu_trial.save(summary_path, changed)
            with self.assertRaisesRegex(ValueError, 'supervisor exit binding changed'):
                adapter.execute(config, 'check', request)
        finally:
            exit_path.write_bytes(raw_exit); summary_path.write_bytes(raw_summary)
        self.assertEqual(adapter.execute(config, 'check', request)['checks'],
                         dict.fromkeys(('build', 'semantics', 'full'), False))
        self.assertEqual(self.calls.count(npu_trial.LAUNCH), 1)

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


    def delayed_exit_fixture(self):
        # Instrument only this test's copied supervisor. Production code has no
        # sleep or testing hook, and the test host pins the copied module bytes.
        path = self.deployment / 'tools/npu_job_supervisor.py'
        code = path.read_text()
        original = '            wait_observation, rc = None, child.wait()\n'
        replacement = (original + "            (folder / 'test-settling-ready').write_text('wait returned')\n"
                       + "            while not (folder / 'test-settling-release').exists(): time.sleep(.01)\n")
        self.assertEqual(code.count(original), 1)
        path.write_text(code.replace(original, replacement))
        return patch.object(npu_trial, '__file__', str(path.with_name('npu_trial.py')))

    def test_dead_child_waits_for_original_supervisor_exit_without_redispatch(self):
        states = []
        def settling_transport(argv, **kwargs):
            if argv[3] == npu_trial.COLLECT and not states:
                remote = Path(self.handles[0]['attempt'])
                deadline = time.monotonic() + 3
                while not (remote / 'test-settling-ready').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((remote / 'test-settling-ready').exists())
            reply = self.transport(argv, **kwargs)
            if argv[3] == npu_trial.COLLECT:
                observed = json.loads(reply['stdout']); states.append(observed['lifecycle_state'])
                if observed['lifecycle_state'] == 'settling':
                    self.assertFalse(observed['alive']); self.assertTrue(observed['supervisor_alive'])
                    self.assertFalse((self.folder / 'result.json').exists())
                    self.assertNotIn('execution_exit', observed)
                    (Path(self.handles[0]['attempt']) / 'test-settling-release').touch()
            return reply
        with self.delayed_exit_fixture():
            result = self.run_trial(settling_transport)
        self.assertEqual(states[0], 'settling'); self.assertEqual(states[-1], 'exited')
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['data']['result_origin'], 'benchmark')
        self.assertEqual(self.calls.count(npu_trial.LAUNCH), 1)

    def test_settling_deadline_stays_unknown_until_original_exit_arrives(self):
        self.config['wait_seconds'] = 0
        def after_child_exit(argv, **kwargs):
            if argv[3] == npu_trial.COLLECT:
                remote = Path(self.handles[0]['attempt'])
                deadline = time.monotonic() + 3
                while not (remote / 'test-settling-ready').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((remote / 'test-settling-ready').exists())
            return self.transport(argv, **kwargs)
        with self.delayed_exit_fixture():
            first = self.run_trial(after_child_exit)
            observed = json.loads((self.folder / 'observation.json').read_bytes())
            self.assertEqual(observed['lifecycle_state'], 'settling')
            self.assertTrue(observed['supervisor_alive']); self.assertFalse(observed['alive'])
            self.assertEqual(first['status'], 'unknown', first)
            self.assertEqual(first['data']['original_handle'], self.handles[0])
            self.assertFalse((self.folder / 'result.json').exists())
            self.assertFalse((self.folder / 'device-result.json').exists())
            (Path(self.handles[0]['attempt']) / 'test-settling-release').touch()
            self.config['wait_seconds'] = 5
            final = self.run_trial()
        self.assertEqual(final['status'], 'completed', final)
        self.assertEqual(final['data']['original_handle'], first['data']['original_handle'])
        self.assertEqual(self.calls.count(npu_trial.LAUNCH), 1)

if __name__ == '__main__': unittest.main()
