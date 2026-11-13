"""New offline preparations freeze isolation without changing task measurements."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/scatter'))
import npu_pipeline_driver as adapter
import npu_trial
from prepare_npu_campaign import prepare, scatter_contract
from validation.npu_isolation_fixture import preparation_profile


class CampaignIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.transport = self.root / 'transport.py'
        self.transport.write_text('raise AssertionError("Preparation must not execute transport")\n')
        self.sequence = 0

    def prepare(self, profile=None):
        if profile is None:
            profile = preparation_profile()
        self.sequence += 1
        profile_path = None
        if profile is not None:
            profile_path = self.root / f'profile-{self.sequence}.json'
            profile_path.write_text(json.dumps(profile))
        output = self.root / f'prepared-{self.sequence}'
        prepare(SimpleNamespace(output=output, task=None, profile=profile_path, runtime_profile=None,
            mode='max', device=0, transport=self.transport, remote_root='/uncontacted/task',
            python=Path(sys.executable), max_model_turns=None))
        return output

    def test_new_policy_required_and_code_closure_pinned_without_measurement_change(self):
        output = self.prepare()
        contract = json.loads((output / 'project/contract.json').read_bytes())
        isolation = contract['measurement'].pop('device_isolation')
        self.assertEqual(contract['measurement'], scatter_contract('max', 0)['measurement'])
        policy = isolation['policy']
        self.assertTrue(policy['required'])
        self.assertEqual(policy['claim'], 'sampled_observed_clear')
        self.assertEqual(policy['scope'], 'cooperative_same_uid_namespace')
        self.assertEqual(policy['logical_device'], 0)
        self.assertEqual(policy['lease_root'], '/tmp/kernelbridge-npu-leases')
        config = json.loads((output / 'pipeline-tools/config.json').read_bytes())
        self.assertEqual(config['device_isolation'], isolation)
        native = json.loads((output / 'trial-config.json').read_bytes())
        self.assertEqual(native['contract']['measurement']['device_isolation'], isolation)
        fields = npu_trial.isolation_request_fields(native)
        for name in ('npu-launch', 'npu-collect'):
            driver = config['drivers'][name]
            self.assertEqual(driver['native_admission_protocol'], 'kernelbridge.native-admission/v1')
            self.assertEqual(set(driver['native_admission_pins']), {
                'supervisor_sha256', 'npu_enter_code_sha256', 'isolation_code_sha256', 'process_isolation_code_sha256'})
            for field, path in driver['native_admission_pins'].items():
                self.assertEqual(driver['files'][path], hashlib.sha256(Path(path).read_bytes()).hexdigest())
                if field != 'supervisor_sha256':
                    self.assertEqual(fields[field], driver['files'][path])
        measure = config['drivers']['npu-measure']
        self.assertIn(measure['isolation_validator'], measure['files'])
        self.assertFalse((output / 'project/candidate.py').exists())

    def test_policy_cannot_be_disabled_or_moved_to_another_device(self):
        for weakened in ({'required': False, 'logical_device': 0},
                         {'logical_device': 1},
                         {'logical_device': 0, 'claim': 'absolute_exclusive'},
                         {'logical_device': 0, 'scope': 'unrestricted'},
                         {'logical_device': 0, 'sample_interval_ms': 1000, 'max_gap_ms': 100}):
            with self.subTest(policy=weakened), self.assertRaises(ValueError):
                self.prepare({'device_isolation': weakened})

    def test_missing_view_fails_before_trial_creation_with_public_initialization_path(self):
        with self.assertRaisesRegex(ValueError, 'NPU PID-view initialization is required') as caught:
            self.prepare({})
        self.assertIn('prepare_npu_isolation.py initialize', str(caught.exception))
        self.assertIn('initialized-profile.json', str(caught.exception))
        self.assertFalse((self.root / f'prepared-{self.sequence}').exists())
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'examples/scatter/prepare_npu_isolation.py'), '--help'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('initialize', result.stdout)
        self.assertIn('recover', result.stdout)

    def test_changed_original_pid_view_is_rejected_before_trial_creation(self):
        profile = preparation_profile()
        self.assertEqual(profile['validation_fixture'], {'kind': 'synthetic_pid_view', 'device_evidence': False})
        policy = profile['device_isolation']['policy']
        policy['view_evidence']['raw_snapshots']['active']['inventory']['stdout'] += '\nchanged original snapshot'
        import npu_isolation
        profile['device_isolation']['policy_sha256'] = npu_isolation.policy_digest(policy)
        with self.assertRaisesRegex(ValueError, 'NPU PID-view initialization is required'):
            self.prepare(profile)
        self.assertFalse((self.root / f'prepared-{self.sequence}').exists())

    def test_bound_contract_cannot_remove_isolation(self):
        output = self.prepare()
        config = json.loads((output / 'trial-config.json').read_bytes())
        contract = copy.deepcopy(config['contract'])
        files = contract['oracle']['files']
        roots = {str(Path(path).parent) for path in files.values()}
        self.assertEqual(len(roots), 1)
        hashes = {name: adapter.sha(path) for name, path in files.items()}
        contract['oracle']['files'] = {'root': roots.pop(), 'files': hashes, 'sha256': adapter.manifest_sha(hashes)}
        adapter.verify_contract(config, {'contract': contract})
        del contract['measurement']['device_isolation']
        with self.assertRaises(adapter.ContractMismatch):
            adapter.verify_contract(config, {'contract': contract})

    def test_legacy_contract_has_no_new_isolation_proof_or_requirement(self):
        config = {'contract': {'measurement': {'device': 0}}}
        self.assertEqual(npu_trial.isolation_request_fields(config), {})
        self.assertIsNone(npu_trial.isolation_projection(config, self.root / 'absent', {}, None))
        self.assertFalse(npu_trial.repeatable_attempt(config, self.root / 'absent', {}))


if __name__ == '__main__':
    unittest.main()
