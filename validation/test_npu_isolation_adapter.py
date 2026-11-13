"""Original NPU receipt recovery and isolated remeasurement, without devices."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/scatter'))
from validation.npu_isolation_fixture import build_admission_fixture, build_fixture, identity, seal, snapshot
import npu_isolation as isolation
import npu_trial as trial
import npu_pipeline_driver as adapter
from ascend_sources import candidate


def encoded(value):
    return (json.dumps(value, indent=2, allow_nan=False) + '\n').encode()


class NativeIsolationAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'candidate.py'
        self.source.write_text(candidate('max', 33))
        self.state = self.root / 'attempts'
        self.folder = self.state / 'attempt-0001'
        self.folder.mkdir(parents=True)
        self.result = {'status': 'completed', 'correct': True, 'source_sha256': adapter.sha(self.source),
            'driver_sha256': adapter.sha(ROOT / 'examples/scatter/npu_benchmark.py'),
            'baseline_sha256': adapter.sha(ROOT / 'examples/scatter/npu_eager.py'),
            'device': 0, 'warmup': 3, 'repeats': 10, 'pairs': 3, 'build_completed': True,
            'environment': {'fixture': 'synthetic receipts, not performance evidence'}, 'soc': 'fixture',
            'geomean_speedup': 1.5, 'cases': [{'id': 'case', 'correct': True, 'candidate': 2.0,
                'eager': 3.0, 'speedup': 1.5, 'input_sha256': 'c' * 64,
                'arms': [{'pair': pair, 'arm': arm, 'us': 3.0 if arm == 'eager' else 2.0}
                         for pair in range(3) for arm in ('eager', 'candidate', 'candidate', 'eager')]}]}
        self.fixture = build_fixture(source_sha256=adapter.sha(self.source),
                                     result_sha256=hashlib.sha256(encoded(self.result)).hexdigest())
        self.intent = {**self.fixture['intent'], 'remote_attempt': self.fixture['request']['remote_attempt'],
                       'hypothesis': 'Synthetic original receipt recovery'}
        self.contract = {'task': {'operator': 'fixture'}, 'suite': [{'id': 'case'}],
            'oracle': {'kind': 'independent', 'description': 'synthetic fixture only',
                'files': {name: str(ROOT / 'examples/scatter' / name) for name in ('npu_benchmark.py', 'npu_eager.py')}},
            'measurement': {'device': 0, 'warmup': 3, 'repeats': 10, 'pairs': 3,
                'device_isolation': {'policy': self.fixture['policy'],
                                     'policy_sha256': isolation.policy_digest(self.fixture['policy'])}}}
        self.config = {'candidate': str(self.source), 'state': str(self.state), 'mode': 'fixture', 'device': 0,
            'remote_root': self.fixture['request']['remote_root'], 'attempt_namespace': 'fixture',
            'wait_seconds': 0, 'poll_seconds': .01, 'contract': self.contract}
        (self.folder / 'candidate.py').write_bytes(self.source.read_bytes())
        (self.folder / 'intent.json').write_bytes(encoded(self.intent))

    def observation(self, verdict='clear'):
        fixture = self.fixture
        if verdict == 'unknown':
            fixture['samples'][2]['error'] = 'Synthetic occupancy query timeout'
        elif verdict == 'foreign':
            sample = fixture['samples'][2]
            sample.update(snapshot([(0, 200), (0, 777)]))
            sample['processes'].append({'reported': {'board': 0, 'chip': 0, 'reported_pid': 777},
                'identity': identity(777), 'lineage': [], 'lineage_source': 'fresh'})
        seal(fixture)
        launch_raw = encoded(fixture['handle'])
        value = {'identity_verified': True, 'lifecycle_state': 'completed', 'alive': False,
            'supervisor_alive': False, 'handle': fixture['handle'], 'result': self.result,
            'result_sha256': fixture['result_sha256'], 'cpu_fallback': False, 'log_tail': '', 'log': None,
            'execution_exit': fixture['exit'], 'original_launch_bytes': base64.b64encode(launch_raw).decode(),
            'original_launch_sha256': hashlib.sha256(launch_raw).hexdigest()}
        for name, prefix in trial.ISOLATION_FILES.items():
            if name in fixture['raw']:
                raw = fixture['raw'][name]
                value[prefix + '_bytes'] = base64.b64encode(raw).decode()
                value[prefix + '_sha256'] = hashlib.sha256(raw).hexdigest()
        (self.folder / 'launch.json').write_bytes(encoded({'handle': fixture['handle']}))
        return value

    def collect(self, verdict='clear'):
        observation = self.observation(verdict)
        with patch.object(trial, 'invoke_remote', return_value=observation):
            return trial.collect(self.config, self.folder, object())

    def request(self, role='F', output='projection'):
        contract = copy.deepcopy(self.contract)
        hashes = {name: adapter.sha(path) for name, path in contract['oracle']['files'].items()}
        contract['oracle']['files'] = {'root': str(ROOT / 'examples/scatter'), 'files': hashes,
                                     'sha256': adapter.manifest_sha(hashes)}
        source_files = {'candidate.py': adapter.sha(self.source)}
        source = {'root': str(self.folder), 'files': source_files, 'sha256': adapter.manifest_sha(source_files)}
        return {'source': source, 'binding': {'source': source['sha256']}, 'contract': contract,
                'action_id': 'project-original', 'role': role, 'output': str(self.root / output)}

    def admission(self):
        fixture = build_admission_fixture(source_sha256=self.fixture['source_sha256'],
            result_sha256=self.fixture['result_sha256'], action_id=self.intent['action_id'])
        documents = {'execution-request.json': fixture['request_raw'],
                     'isolation-samples.jsonl': fixture['samples_raw'], 'admission.json': fixture['admission_raw']}
        for name, data in documents.items():
            (self.folder / name).write_bytes(data)
        return documents

    def test_clear_original_keeps_correctness_and_projects_four_identical_raw_receipts(self):
        outcome = self.collect()
        self.assertEqual(outcome['status'], 'completed')
        self.assertTrue(outcome['data']['correct'])
        self.assertEqual(outcome['data']['device_isolation']['verdict'], 'sampled_observed_clear')
        before = (self.folder / 'device-result.json').read_bytes()
        check = adapter.execute(self.config, 'check', self.request())
        self.assertEqual(check['checks'], {'build': True, 'semantics': True, 'full': True})
        proofs = []
        for arm in range(4):
            request = self.request(role='ABBA'[arm], output=f'pair/arm-{arm}')
            if request['role'] == 'A':
                files = {'npu_eager.py': self.result['baseline_sha256']}
                request['source'] = {'root': str(ROOT / 'examples/scatter'), 'files': files,
                                     'sha256': adapter.manifest_sha(files)}
            measured = adapter.execute(self.config, 'measure', request)
            self.assertEqual(measured['new_measurements'], 0)
            self.assertEqual(measured['cases']['case'], [3.0 if request['role'] == 'A' else 2.0] * 3)
            proof = measured['device_isolation']
            proofs.append(proof)
            for name, expected in proof['artifacts'].items():
                copied = Path(request['output']) / name
                self.assertEqual(copied.read_bytes(), self.fixture['raw'][name])
                self.assertEqual(adapter.sha(copied), expected)
        self.assertTrue(all(proof == proofs[0] for proof in proofs))
        self.assertEqual((self.folder / 'device-result.json').read_bytes(), before)
        self.assertFalse(trial.repeatable_attempt(self.config, self.folder, self.intent))

    def test_isolation_unknown_keeps_real_F_and_all_original_timings(self):
        outcome = self.collect('unknown')
        self.assertTrue(outcome['data']['correct'])
        self.assertEqual(outcome['data']['device_isolation']['verdict'], 'unknown')
        self.assertEqual(json.loads((self.folder / 'device-result.json').read_bytes()), self.result)
        check = adapter.execute(self.config, 'check', self.request())
        self.assertTrue(check['checks']['full'])
        self.assertEqual(check['device_isolation']['verdict'], 'unknown')
        self.assertTrue(trial.repeatable_attempt(self.config, self.folder, self.intent))

    def test_proof_read_failure_does_not_hide_original_correctness(self):
        self.collect()
        target = self.folder / 'isolation-receipt.json'
        target.unlink()
        target.symlink_to(self.root / 'absent-proof')
        check = adapter.execute(self.config, 'check', self.request())
        self.assertTrue(check['checks']['full'])
        self.assertEqual(check['device_isolation']['verdict'], 'unknown')
        self.assertEqual(check['device_isolation']['artifacts'], {})

    def test_remote_admission_collect_retains_proof_without_manufacturing_a_result(self):
        documents = self.admission()
        observed = {'status': 'not_started', 'lifecycle_state': 'not_started',
                    'handle': None, 'identity_verified': True, 'alive': False}
        for name, raw in documents.items():
            field = trial.ISOLATION_FILES[name]
            observed[field + '_bytes'] = base64.b64encode(raw).decode()
            observed[field + '_sha256'] = hashlib.sha256(raw).hexdigest()
            (self.folder / name).unlink()
        with patch.object(trial, 'invoke_remote', return_value=observed):
            outcome = trial.collect(self.config, self.folder, object())
        self.assertEqual(outcome['status'], 'not_started')
        self.assertFalse((self.folder / 'result.json').exists())
        self.assertFalse((self.folder / 'device-result.json').exists())
        for name, raw in documents.items():
            self.assertEqual((self.folder / name).read_bytes(), raw)

    def test_not_started_recovery_uses_original_bytes_without_transport(self):
        documents = self.admission()
        with patch.object(trial, 'transport', side_effect=AssertionError('No transport needed for retained admission')):
            outcome = trial.trial(self.config, recover=1)
        self.assertEqual(outcome['status'], 'not_started')
        self.assertIsNone(outcome['data']['original_handle'])
        self.assertFalse((self.folder / 'result.json').exists())
        request = self.request(output='refusal')
        request['action_id'] = self.intent['action_id']
        envelope = adapter.admission_projection(self.config, request, outcome)
        self.assertEqual(envelope['dispatch_outcome'], 'not_started')
        self.assertFalse(envelope['dispatched'])
        for name, raw in documents.items():
            self.assertEqual((Path(request['output']) / 'admission' / name).read_bytes(), raw)

    def test_no_effect_admission_permits_new_action_with_the_same_implementation(self):
        self.admission()
        with patch.object(trial, 'transport', return_value=object()), \
             patch.object(trial, 'invoke_remote', side_effect=RuntimeError('Synthetic dispatch timeout')) as remote:
            outcome = trial.trial(self.config, {'id': 'explicit-after-admission', 'arguments': {}})
        self.assertEqual(remote.call_count, 2)
        self.assertEqual(outcome['status'], 'unknown')
        intent = json.loads((self.state / 'attempt-0002/intent.json').read_bytes())
        self.assertEqual(intent['source_sha256'], self.intent['source_sha256'])
        self.assertEqual(intent['measurement_retry_of'][0]['reason'], 'not_started')
        self.assertTrue((self.folder / 'admission.json').is_file())
        self.assertFalse((self.folder / 'result.json').exists())

    def test_not_started_requires_original_policy_and_entry_code_pin(self):
        self.admission()
        path = self.folder / 'execution-request.json'
        request = json.loads(path.read_bytes())
        request['npu_enter_code_sha256'] = '0' * 64
        path.write_bytes(encoded(request))
        with self.assertRaisesRegex(ValueError, 'npu_enter_code_sha256'):
            trial.admission_outcome(self.config, self.folder, self.intent)

    def test_unknown_dispatch_does_not_become_not_started_or_allow_replacement(self):
        observation = self.observation()
        observation.update(status='not_started', lifecycle_state='not_started')
        with patch.object(trial, 'invoke_remote', return_value=observation):
            outcome = trial.collect(self.config, self.folder, object())
        self.assertEqual(outcome['status'], 'unknown')
        self.assertFalse((self.folder / 'result.json').exists())
        with patch.object(trial, 'invoke_remote', side_effect=AssertionError('Unknown dispatch must not repeat')):
            outcome = trial.trial(self.config, {'id': 'replacement', 'arguments': {}})
        self.assertEqual(outcome['status'], 'unknown')

    def test_nonqualifying_terminal_allows_explicit_same_source_retry_and_preserves_unknown_fence(self):
        self.collect('foreign')
        original = (self.folder / 'device-result.json').read_bytes()
        self.assertTrue(trial.repeatable_attempt(self.config, self.folder, self.intent))
        with patch.object(trial, 'transport', return_value=object()), \
             patch.object(trial, 'invoke_remote', side_effect=RuntimeError('Synthetic lost acknowledgement')) as remote:
            outcome = trial.trial(self.config, {'id': 'explicit-remeasurement', 'arguments': {}})
        self.assertEqual(remote.call_count, 2)  # one attempted launch, then original-only collection
        self.assertEqual(outcome['status'], 'unknown')
        newer = json.loads((self.state / 'attempt-0002/intent.json').read_bytes())
        self.assertEqual(newer['source_sha256'], self.intent['source_sha256'])
        self.assertEqual(newer['measurement_retry_of'][0]['action_id'], self.intent['action_id'])
        self.assertEqual((self.folder / 'device-result.json').read_bytes(), original)
        with patch.object(trial, 'transport', side_effect=AssertionError('Pending original remains fenced')):
            outcome = trial.trial(self.config, {'id': 'third-action', 'arguments': {}})
        self.assertEqual(outcome['status'], 'unknown')
        self.assertFalse((self.state / 'attempt-0003').exists())

    def test_round_lookup_prefers_exact_original_action_and_full_handle(self):
        self.collect('unknown')
        second = self.state / 'attempt-0002'; second.mkdir()
        other = {**self.intent, 'attempt': 2, 'action_id': 'second-E'}
        (second / 'intent.json').write_bytes(encoded(other))
        handle = {**self.fixture['handle'], 'pid': 999}
        (second / 'result.json').write_bytes(encoded({'original_handle': handle}))
        request = self.request()
        request['action_id'] = self.intent['action_id']
        self.assertEqual(adapter.round_for(self.config, request)[0], self.folder)
        request['action_id'] = 'F-on-second'
        self.assertEqual(adapter.round_for(self.config, request)[0], second)
        request.update(handle=handle)
        self.assertEqual(adapter.round_for(self.config, request)[0], second)
        request['handle'] = {**handle, 'argv': ['/bin/other']}
        with self.assertRaises(ValueError):
            adapter.round_for(self.config, request)


if __name__ == '__main__':
    unittest.main()
