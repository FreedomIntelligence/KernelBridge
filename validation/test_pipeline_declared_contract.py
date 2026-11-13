"""Offline A-stage contract binding: no compiler, model, transport, or device."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/operator_pipeline'))
from isolation import policy_digest
from pipeline import DECLARED_CONTRACT_SCHEMA, Pipeline, digest, main
from prepare import prepare


class DeclaredContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.oracle = self.root / 'oracle.py'
        self.oracle.write_text('def oracle(values):\n    return sum(values)\n')
        self.candidate = self.root / 'candidate.py'
        self.candidate.write_text('def candidate(values):\n    return sum(values)\n')
        policy = {'schema_version': 'kernelbridge.npu-isolation-policy/v1',
                  'required': True, 'logical_device': 1,
                  'view_evidence': {'scope': 'offline binding fixture', 'source': 'x' * 9500}}
        self.isolation = {'policy': policy, 'policy_sha256': policy_digest(policy)}
        self.contract = {
            'task': {'operator': 'fixture sum', 'outputs': 'one scalar'},
            'oracle': {'kind': 'independent', 'description': 'Independent fixture reference',
                       'files': {'oracle.py': str(self.oracle)}},
            'suite': [{'id': 'empty', 'rows': 0}, {'id': 'nonempty', 'rows': 7}],
            'measurement': {'scope': 'offline_fixture', 'device': 1, 'repeats': 10,
                            'device_isolation': self.isolation},
            'tolerance': {'rtol': 0.0, 'atol': 0.0},
            'qualification': 'Complete original fixture contract',
        }

    def tearDown(self):
        self.temporary.cleanup()

    def prepared(self, *, declared=True, isolation=True, contract=None, name=None):
        profile = {'read_roots': [str(self.root)], 'drivers': {}}
        if isolation:
            profile['device_isolation'] = copy.deepcopy(self.isolation)
        if declared:
            profile['declared_contract'] = copy.deepcopy(self.contract if contract is None else contract)
        output = self.root / (name or uuid.uuid4().hex) / 'tools'
        tools = prepare(profile, output, sys.executable)
        return json.loads((output / 'config.json').read_text()), tools

    def call(self, config, tool='pipeline_bind', **arguments):
        return main(config, {'id': uuid.uuid4().hex, 'tool': tool, 'arguments': arguments})

    def assert_rejected_before_bind(self, config, arguments, *, path=None):
        before = Pipeline(config).state
        with patch.object(Pipeline, 'freeze', side_effect=AssertionError('Rejection must precede source freeze')):
            outcome = self.call(config, **arguments)
        self.assertEqual(outcome['status'], 'failed', outcome)
        after = Pipeline(config).state
        self.assertEqual({k: v for k, v in after.items() if k != 'receipts'},
                         {k: v for k, v in before.items() if k != 'receipts'})
        self.assertFalse(list(Path(config['state']).rglob('launch-intent.json')))
        self.assertFalse(list(Path(config['state']).rglob('effect-intent.json')))
        if path is not None:
            result = outcome['data']['result']
            self.assertEqual(result['failure_phase'], 'bind')
            self.assertEqual(result['dispatch_outcome'], 'not_started')
            self.assertIn(path, [row['path'] for row in result['binding_rejection']['differences']])
        return outcome

    def test_legacy_manual_profile_keeps_schema_and_contract_semantics(self):
        contract = copy.deepcopy(self.contract)
        contract['measurement'].pop('device_isolation')
        # Legacy generic profiles allowed non-string nonempty case IDs.
        contract['suite'][0]['id'] = 1
        config, tools = self.prepared(declared=False, isolation=False)
        bind = next(tool for tool in tools if tool['name'] == 'pipeline_bind')
        self.assertEqual(bind['parameters']['required'], ['contract'])
        self.assertNotIn('selection', bind['parameters']['properties'])
        outcome = self.call(config, contract=contract)
        self.assertEqual(outcome['status'], 'completed', outcome)
        self.assertEqual(outcome['data']['result']['selection'], 'explicit')
        self.assertIs(outcome['data']['result']['verified_against_declared'], False)
        self.assertEqual(Pipeline(config).state['contract']['suite'], contract['suite'])

    def test_declared_selection_freezes_entire_contract_without_model_copy(self):
        config, tools = self.prepared()
        capsule = config['declared_contract']
        self.assertEqual(capsule['schema_version'], DECLARED_CONTRACT_SCHEMA)
        self.assertEqual(capsule['contract_sha256'], digest(self.contract))
        self.assertEqual(capsule['oracle_files_sha256'], {'oracle.py': hashlib.sha256(self.oracle.read_bytes()).hexdigest()})
        self.assertEqual(capsule['contract'], self.contract)
        tool = next(tool for tool in tools if tool['name'] == 'pipeline_bind')
        self.assertEqual(tool['parameters']['properties']['selection']['enum'], ['declared'])
        self.assertIn('exactly one', tool['description'])
        # Invoke the actual frozen tool entry, not only the in-process helper.
        reply = subprocess.run(tool['argv'], input=json.dumps({'id': 'declared-entry',
            'tool': 'pipeline_bind', 'arguments': {'selection': 'declared'}}),
            text=True, capture_output=True, check=True)
        outcome = json.loads(reply.stdout)
        self.assertEqual(outcome['status'], 'completed', outcome)
        self.assertEqual(outcome['data']['result']['declared_contract_sha256'], capsule['contract_sha256'])
        result = outcome['data']['result']
        self.assertIs(result['verified_against_declared'], True)
        self.assertNotEqual(result['contract_sha256'], result['declared_contract_sha256'])
        self.assertEqual(result['contract_sha256_scope'], 'bound_contract_with_frozen_oracle_manifest')
        self.assertEqual(result['declared_contract_sha256_scope'], 'declared_contract_with_original_oracle_paths')
        self.assertIn('does not require rebinding', result['binding_note'])
        bound = Pipeline(config).state['contract']
        self.assertEqual(bound['measurement'], self.contract['measurement'])
        self.assertEqual(bound['suite'], self.contract['suite'])
        self.assertEqual((Path(bound['oracle']['files']['root']) / 'oracle.py').read_bytes(), self.oracle.read_bytes())

    def test_explicit_exact_contract_and_equal_bytes_at_another_path_are_accepted(self):
        for use_copy in (False, True):
            with self.subTest(use_copy=use_copy):
                config, _ = self.prepared()
                contract = copy.deepcopy(self.contract)
                if use_copy:
                    duplicate = self.root / 'same-oracle.py'
                    duplicate.write_bytes(self.oracle.read_bytes())
                    contract['oracle']['files']['oracle.py'] = str(duplicate)
                outcome = self.call(config, contract=contract)
                self.assertEqual(outcome['status'], 'completed', outcome)
                self.assertEqual(outcome['data']['result']['selection'], 'explicit')

    def test_every_declared_field_is_checked_before_freeze(self):
        changes = [
            ('/measurement/device_isolation', lambda c: c['measurement'].pop('device_isolation')),
            ('/measurement/repeats', lambda c: c['measurement'].update(repeats=11)),
            ('/measurement/repeats', lambda c: c['measurement'].update(repeats=10.0)),
            ('/task/outputs', lambda c: c['task'].update(outputs='different output')),
            ('/suite/1/rows', lambda c: c['suite'][1].update(rows=8)),
            ('/tolerance/rtol', lambda c: c['tolerance'].update(rtol=0.1)),
            ('/qualification', lambda c: c.pop('qualification')),
            ('/added~1field~0', lambda c: c.update({'added/field~': 'unapproved'})),
        ]
        for path, change in changes:
            with self.subTest(path=path):
                config, _ = self.prepared()
                contract = copy.deepcopy(self.contract)
                change(contract)
                self.assert_rejected_before_bind(config, {'contract': contract}, path=path)

    def test_different_oracle_bytes_are_rejected_with_exact_file_pointer(self):
        config, _ = self.prepared()
        other = self.root / 'different.py'
        other.write_text('def oracle(values):\n    return 0\n')
        contract = copy.deepcopy(self.contract)
        contract['oracle']['files']['oracle.py'] = str(other)
        self.assert_rejected_before_bind(config, {'contract': contract}, path='/oracle/files/oracle.py')

    def test_changed_original_oracle_and_declared_capsule_are_rejected(self):
        for mutation in ('oracle', 'contract', 'file_set', 'schema'):
            with self.subTest(mutation=mutation):
                config, _ = self.prepared()
                original = self.oracle.read_bytes()
                if mutation == 'oracle':
                    self.oracle.write_bytes(original + b'# changed after preparation\n')
                elif mutation == 'contract':
                    config['declared_contract']['contract']['measurement']['repeats'] = 11
                elif mutation == 'file_set':
                    config['declared_contract']['oracle_files_sha256']['extra.py'] = 'a' * 64
                else:
                    config['declared_contract']['schema_version'] = 'other'
                try:
                    outcome = self.assert_rejected_before_bind(config, {'selection': 'declared'})
                    self.assertIn('Frozen declared', outcome['data']['result']['error'])
                finally:
                    self.oracle.write_bytes(original)

    def test_oracle_change_between_comparison_and_capture_is_rejected(self):
        config, _ = self.prepared()
        freeze = Pipeline.freeze
        def racing_freeze(instance, files, folder):
            self.oracle.write_bytes(self.oracle.read_bytes() + b'# concurrent source edit\n')
            return freeze(instance, files, folder)
        with patch.object(Pipeline, 'freeze', racing_freeze):
            outcome = self.call(config, selection='declared')
        self.assertEqual(outcome['status'], 'failed', outcome)
        self.assertEqual(outcome['data']['result']['dispatch_outcome'], 'not_started')
        self.assertEqual(outcome['data']['result']['binding_rejection']['differences'][0]['path'], '/oracle/files/oracle.py')
        self.assertIsNone(Pipeline(config).state['contract'])
        self.assertEqual(Pipeline(config).state['revision'], 0)

    def test_failed_rebind_preserves_candidate_check_pair_and_revision(self):
        config, _ = self.prepared()
        self.assertEqual(self.call(config, selection='declared')['status'], 'completed')
        self.call(config, 'pipeline_plan', stage='C', record={'hypothesis': 'fixture'})
        self.assertEqual(self.call(config, 'pipeline_freeze', files={'candidate.py': str(self.candidate)},
                                   origin={'scope': 'offline fixture'})['status'], 'completed')
        instance = Pipeline(config)
        # Sentinel historical acceptance belongs to the previously bound state;
        # no test driver or device result is being asserted here.
        instance.state.update(check={'receipt': 'original-F', 'passed': True},
                              pair={'receipts': ['A1', 'B1', 'B2', 'A2']},
                              incumbent=copy.deepcopy(instance.state['candidate']), stage='H')
        before = copy.deepcopy(instance.state)
        changed = copy.deepcopy(self.contract)
        changed['measurement'].pop('device_isolation')
        outcome = instance.apply({'id': uuid.uuid4().hex, 'tool': 'pipeline_bind', 'arguments': {'contract': changed}})
        self.assertEqual(outcome['status'], 'failed', outcome)
        after = Pipeline(config).state
        for key in ('revision', 'stage', 'contract', 'candidate', 'check', 'pair', 'incumbent', 'C'):
            self.assertEqual(after[key], before[key], key)

    def test_mutually_exclusive_selection_and_missing_declared_profile(self):
        config, _ = self.prepared()
        for args in ({}, {'selection': 'other'}, {'selection': 'declared', 'contract': self.contract},
                     {'contract': self.contract, 'unknown': True}):
            with self.subTest(keys=list(args)):
                self.assert_rejected_before_bind(config, args)
        legacy, _ = self.prepared(declared=False)
        result = self.assert_rejected_before_bind(legacy, {'selection': 'declared'})
        self.assertIn('no declared contract', result['data']['result']['error'])

    def test_legacy_isolation_profile_rejects_missing_policy_at_bind(self):
        config, _ = self.prepared(declared=False)
        changed = copy.deepcopy(self.contract)
        changed['measurement'].pop('device_isolation')
        outcome = self.assert_rejected_before_bind(config, {'contract': changed})
        self.assertIn('/measurement/device_isolation', outcome['data']['result']['error'])

    def test_prepare_rejects_wrong_isolation_binding_and_external_oracle(self):
        changed = copy.deepcopy(self.contract)
        changed['measurement'].pop('device_isolation')
        with self.assertRaisesRegex(ValueError, 'Declared isolation policy'):
            self.prepared(contract=changed)
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external) / 'oracle.py'
            outside.write_bytes(self.oracle.read_bytes())
            changed = copy.deepcopy(self.contract)
            changed['oracle']['files']['oracle.py'] = str(outside)
            with self.assertRaisesRegex(ValueError, 'configured read roots'):
                self.prepared(contract=changed)

    def test_large_mismatch_keeps_complete_hash_bound_paginated_evidence(self):
        config, _ = self.prepared()
        changed = copy.deepcopy(self.contract)
        for number in range(14):
            changed['extra-' + str(number)] = {'contents': 'changed' * 100}
        outcome = self.assert_rejected_before_bind(config, {'contract': changed})
        rejection = outcome['data']['result']['binding_rejection']
        self.assertEqual(len(rejection['differences']), 8)
        self.assertEqual(rejection['difference_count'], 14)
        self.assertTrue(rejection['truncated'])
        folder = Path(config['state']) / outcome['data']['run']
        for name, identity in rejection['artifacts'].items():
            raw = (folder / name).read_bytes()
            self.assertEqual(identity, {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)})
        self.assertEqual(len(json.loads((folder / 'bind-differences.json').read_bytes())), 14)
        page = self.call(config, 'pipeline_read', run=outcome['data']['run'], path='bind-differences.json', limit=512)
        self.assertEqual(page['status'], 'completed', page)
        self.assertEqual(page['data']['sha256'], rejection['artifacts']['bind-differences.json']['sha256'])

    def test_status_recommends_only_available_bind_form(self):
        for declared in (False, True):
            with self.subTest(declared=declared):
                config, _ = self.prepared(declared=declared)
                outcome = self.call(config, 'pipeline_status')
                step = outcome['data']['result']['workflow']['next_actions'][0]
                self.assertEqual(step['tool'], 'pipeline_bind')
                if declared:
                    self.assertEqual(step['arguments'], {'selection': 'declared'})
                else:
                    self.assertIn('contract', step['required_arguments'])

    def test_npu_preparation_declares_unchanged_contract_and_explicit_bind_entry(self):
        path = ROOT / 'examples/scatter/prepare_npu_campaign.py'
        spec = importlib.util.spec_from_file_location('declared_contract_npu_prepare', path)
        campaign = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(campaign)
        transport = self.root / 'transport.py'
        transport.write_text('raise AssertionError("Offline preparation must never import transport")\n')
        from validation.npu_isolation_fixture import preparation_profile
        profile_path = self.root / 'synthetic-pid-view.json'
        profile_path.write_text(json.dumps(preparation_profile(device=1)))
        args = SimpleNamespace(output=self.root / 'campaign', task=None, profile=profile_path, runtime_profile=None,
            mode='min', device=1, transport=transport, remote_root='/offline/no-device', python=Path(sys.executable))
        with patch.object(subprocess, 'run', side_effect=AssertionError('No remote or device execution')):
            campaign.prepare(args)
        task = json.loads((args.output / 'task.json').read_text())
        profile = json.loads((args.output / 'workflow-profile.json').read_text())
        config = json.loads((args.output / 'pipeline-tools/config.json').read_text())
        expected = campaign.scatter_contract('min', 1)
        self.assertEqual(task['contract']['suite'], expected['suite'])
        self.assertEqual({k: v for k, v in task['contract']['measurement'].items() if k != 'device_isolation'},
                         expected['measurement'])
        self.assertEqual(profile['declared_contract'], task['contract'])
        self.assertEqual(config['declared_contract']['contract'], task['contract'])
        inputs = json.loads((args.output / 'project/workflow-inputs.json').read_text())
        self.assertEqual(inputs['bind'], {'selection': 'declared'})
        self.assertEqual(inputs['contract'], task['contract'])
        self.assertFalse((args.output / 'project/candidate.py').exists())
        outcome = self.call(config, selection='declared')
        self.assertEqual(outcome['status'], 'completed', outcome)


if __name__ == '__main__':
    unittest.main()
