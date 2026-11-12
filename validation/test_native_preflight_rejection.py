"""Native preflight proof and immutable preparation; no model/NPU/transport."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/scatter'))
sys.path.insert(0, str(ROOT / 'examples/operator_pipeline'))
import npu_pipeline_driver as adapter
import preflight
from pipeline import Pipeline, main
from prepare_npu_campaign import prepare


class NativePreflightRejectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.oracle = self.root / 'oracle.py'; self.oracle.write_text('# independent fixture oracle\n')
        self.candidate = self.root / 'candidate.py'; self.candidate.write_text('# no operator implementation\n')
        self.source = {'root': str(self.root), 'files': {'candidate.py': adapter.sha(self.candidate)}}
        self.source['sha256'] = adapter.manifest_sha(self.source['files'])
        oracle = {'root': str(self.root), 'files': {'oracle.py': adapter.sha(self.oracle)}}
        oracle['sha256'] = adapter.manifest_sha(oracle['files'])
        self.contract = {'task': {'operator': 'fixture'}, 'suite': [{'id': 'all'}],
            'oracle': {'kind': 'independent', 'description': 'fixture', 'files': oracle},
            'measurement': {'scope': 'local_protocol_fixture', 'device': 1, 'warmup': 3, 'repeats': 10, 'pairs': 3}}
        declared = copy.deepcopy(self.contract); declared['oracle']['files'] = {'oracle.py': str(self.oracle)}
        transport = self.root / 'transport.py'
        transport.write_text('raise AssertionError("transport must not even be imported")\n')
        self.config = {'contract': declared, 'state': str(self.root / 'attempts'),
            'candidate': str(self.root / 'project/candidate.py'), 'pipeline_state': str(self.root / 'pipeline'),
            'transport_module': str(transport)}
        self.output = self.root / 'driver'; self.output.mkdir()
        self.request = {'action_id': 'fixture/phase/2-E', 'source': self.source,
            'contract': copy.deepcopy(self.contract), 'role': 'E', 'handle': None,
            'binding': {'source': self.source['sha256'], 'contract': preflight.digest(self.contract),
                        'baseline': None, 'revision': 1}, 'output': str(self.output)}

    def rejection(self, request=None, config=None):
        request = request or self.request
        with patch.object(adapter, 'trial', side_effect=AssertionError('no trial')) as trial, \
                patch.object(adapter, 'transport', side_effect=AssertionError('no transport')) as transport, \
                patch.object(adapter, 'invoke_remote', side_effect=AssertionError('no remote')) as remote:
            value = adapter.execute(config or self.config, 'launch', request)
            trial.assert_not_called(); transport.assert_not_called(); remote.assert_not_called()
        self.assertEqual(value['dispatch_outcome'], 'not_started')
        self.assertIs(preflight.validate_rejection(value, request, Path(request['output'])), value)
        self.assertFalse((Path(request['output']) / 'dispatch-boundary.json').exists())
        for name, expected in value['evidence']['artifacts'].items():
            self.assertEqual(adapter.sha(Path(request['output']) / name), expected)
        return value

    def test_original_max_request_replays_two_extra_fields_without_dispatch(self):
        audit = Path(os.environ.get('KERNELBRIDGE_PREFLIGHT_REJECTION_EVIDENCE', str(ROOT /
            '.validation/hongkong-resume-20261111/trial-analysis/max/first-local-validation-failure.json')))
        if not audit.exists():
            self.skipTest('Original private max receipt is unavailable; supply its local evidence path')
        evidence = json.loads(audit.read_text())
        originals = {ROOT / name: expected for name, expected in evidence['evidence_files'].items()}
        for path, expected in originals.items(): self.assertEqual(adapter.sha(path), expected)
        request_path = next(path for path in originals if path.name == 'request.json')
        request = json.loads(request_path.read_text()); request['output'] = str(self.output)
        trial_root = request_path.parents[3]
        config = json.loads((trial_root / 'trial-config.json').read_text())
        value = self.rejection(request, config)
        self.assertEqual(value['error']['differences'], [
            {'path': '/baseline', 'kind': 'extra'}, {'path': '/candidate_abi', 'kind': 'extra'}])
        self.assertEqual(value['request_sha256'], preflight.digest(request))
        self.assertEqual(value['source_sha256'], request['source']['sha256'])
        self.assertEqual(value['binding'], request['binding'])
        self.assertIn('/baseline', value['error']['message'])
        self.assertIn('/candidate_abi', value['error']['message'])
        for path, expected in originals.items(): self.assertEqual(adapter.sha(path), expected)

    def test_cube_and_arbitrary_contracts_use_the_same_strict_comparison(self):
        for index, task in enumerate(({'operator': 'cube matmul', 'shape': [128, 128, 128]},
                                      {'unrelated/domain': {'enabled': True, 'label': '自定义'}})):
            with self.subTest(task=task):
                config = copy.deepcopy(self.config); config['contract']['task'] = task
                request = copy.deepcopy(self.request); request['contract']['task'] = copy.deepcopy(task)
                request['contract']['extra-metadata'] = {'not': 'ignored'}
                request['output'] = str(self.root / f'generic-{index}')
                value = self.rejection(request, config)
                self.assertEqual(value['error']['differences'], [{'path': '/extra-metadata', 'kind': 'extra'}])

    def test_json_pointer_nested_types_values_and_list_members(self):
        expected = {'a/b~': {'missing': 7, 'bool': True, 'number': 1, 'value': 'a'},
                    'shorter': [0, 1], 'longer': [0]}
        actual = {'a/b~': {'extra': 8, 'bool': 1, 'number': 1.0, 'value': 'b'},
                  'shorter': [0], 'longer': [0, 2]}
        changes = {(row['path'], row['kind']) for row in preflight.contract_differences(expected, actual)}
        self.assertEqual(changes, {('/a~1b~0/missing', 'missing'), ('/a~1b~0/extra', 'extra'),
            ('/a~1b~0/bool', 'type'), ('/a~1b~0/number', 'type'), ('/a~1b~0/value', 'value'),
            ('/shorter/1', 'missing'), ('/longer/1', 'extra')})
        self.assertEqual(preflight.contract_differences(-0.0, 0.0)[0]['kind'], 'value')
        for nonfinite in (float('nan'), float('inf'), -float('inf')):
            with self.subTest(nonfinite=nonfinite), self.assertRaises(ValueError):
                preflight.contract_differences({'value': 1}, {'value': nonfinite})

    def test_large_unicode_difference_has_bounded_summary_and_complete_evidence(self):
        key = '完整🙂/' * 200
        value = '原始中文🙂' * 5000
        self.request['contract'].update({key + str(index): value for index in range(12)})
        result = self.rejection()
        self.assertEqual(result['error']['difference_count'], 12)
        self.assertTrue(result['error']['truncated'])
        self.assertLess(len(preflight.encoded(result)), 16000)
        self.assertLessEqual(len(result['error']['message'].encode()), 2048)
        full = json.loads((self.output / 'preflight/differences.json').read_text())
        self.assertEqual(len(full), 12)
        self.assertTrue(all(row['actual'] == value for row in full))
        self.assertGreater((self.output / 'preflight/differences.json').stat().st_size, 16000)
        self.assertNotIn('\ufffd', (self.output / 'preflight/differences.json').read_text())

    def test_request_identity_and_artifact_tampering_cannot_reuse_proof(self):
        self.request['contract']['extra'] = True; value = self.rejection()
        for field, replacement in (('action_id', 'other'), ('source_sha256', '0' * 64),
                                   ('request_sha256', '0' * 64), ('operation', 'collect')):
            changed = copy.deepcopy(value); changed[field] = replacement
            with self.subTest(field=field), self.assertRaises(ValueError):
                preflight.validate_rejection(changed, self.request, self.output)
        changed = copy.deepcopy(value); changed['handle'] = None
        with self.assertRaises(ValueError): preflight.validate_rejection(changed, self.request, self.output)
        path = self.output / 'preflight/differences.json'; original = path.read_bytes()
        path.write_bytes(b'[]\n')
        with self.assertRaises(ValueError): preflight.validate_rejection(value, self.request, self.output)
        changed = copy.deepcopy(value); changed['evidence']['artifacts']['preflight/differences.json'] = adapter.sha(path)
        with self.assertRaises(ValueError): preflight.validate_rejection(changed, self.request, self.output)
        path.write_bytes(original)
        (self.output / 'dispatch-boundary.json').write_text('{}')
        with self.assertRaises(ValueError): preflight.validate_rejection(value, self.request, self.output)

    def test_source_or_oracle_identity_failures_do_not_become_contract_rejections(self):
        self.request['contract']['extra'] = True
        for path in (self.candidate, self.oracle):
            original = path.read_bytes(); path.write_bytes(original + b'# changed\n')
            try:
                with patch.object(adapter, 'trial') as trial, self.assertRaisesRegex(ValueError, 'Frozen source changed'):
                    adapter.execute(self.config, 'launch', self.request)
                trial.assert_not_called()
                self.assertFalse((self.output / 'preflight').exists())
            finally:
                path.write_bytes(original)

    def test_contract_error_after_effect_boundary_is_not_caught_as_preflight(self):
        pipeline = Path(self.config['pipeline_state']); pipeline.mkdir()
        (pipeline / 'event-00000001.json').write_text(json.dumps({'body': {'C': {'hypothesis': 'fixture'}}}))
        error = adapter.ContractMismatch({'expected': 1}, {'actual': 2})
        with patch.object(adapter, 'trial', side_effect=error) as trial, \
                patch.object(adapter, 'transport') as transport, self.assertRaises(adapter.ContractMismatch):
            adapter.execute(self.config, 'launch', self.request)
        trial.assert_called_once(); transport.assert_not_called()
        boundary = json.loads((self.output / 'dispatch-boundary.json').read_text())
        self.assertEqual(boundary['request_sha256'], preflight.digest(self.request))
        self.assertFalse((self.output / 'preflight').exists())

    def pipeline(self, *, opt_in, post_boundary=False):
        native_config = self.root / 'native-config.json'; native_config.write_text(json.dumps(self.config))
        driver = ROOT / 'examples/scatter/npu_pipeline_driver.py'
        if post_boundary:
            driver = self.root / 'post-boundary-driver.py'
            driver.write_text('import json,sys\nfrom pathlib import Path\n'
                + 'sys.path.insert(0,' + repr(str(ROOT / 'examples/scatter')) + ')\n'
                + 'import npu_pipeline_driver as adapter\n'
                + 'def stopped(*args,**kwargs):\n'
                + ' raise adapter.ContractMismatch({"expected":1},{"actual":2})\n'
                + 'adapter.trial=stopped\n'
                + 'print(json.dumps(adapter.execute(json.loads(Path(sys.argv[1]).read_text()),"launch",json.load(sys.stdin))))\n')
        files = [driver, native_config, ROOT / 'examples/scatter/npu_pipeline_driver.py',
                 ROOT / 'examples/operator_pipeline/preflight.py']
        spec = {'kind': 'launch', 'argv': [sys.executable, '-B', str(driver), str(native_config), 'launch'],
                'files': {str(p): adapter.sha(p) for p in files}, 'timeout': 10}
        if opt_in: spec['preflight_rejection_protocol'] = preflight.PROTOCOL
        config = {'state': self.config['pipeline_state'], 'read_roots': [str(self.root)], 'drivers': {'launch': spec}}
        pipeline = Pipeline(config)
        contract = copy.deepcopy(self.config['contract'])
        if not post_boundary: contract['extra'] = True
        for tool, arguments in [('pipeline_bind', {'contract': contract}),
                ('pipeline_plan', {'stage': 'C', 'record': {'hypothesis': 'fixture'}}),
                ('pipeline_freeze', {'files': {'candidate.py': str(self.candidate)}, 'origin': {'scope': 'fixture'}})]:
            reply = pipeline.apply({'id': uuid.uuid4().hex, 'tool': tool, 'arguments': arguments})
            self.assertEqual(reply['status'], 'completed', reply)
        return pipeline

    def test_legacy_unopted_native_reply_remains_unknown(self):
        pipeline = self.pipeline(opt_in=False)
        reply = pipeline.apply({'id': 'legacy-native', 'tool': 'pipeline_run',
                                'arguments': {'stage': 'E', 'driver': 'launch'}})
        self.assertEqual(reply['status'], 'failed')
        self.assertEqual(reply['data']['result']['launch_outcome'], 'unknown')
        self.assertEqual(len(Pipeline(pipeline.config).pending_launches()), 1)
        self.assertNotIn('launch_rejections', pipeline.state)
        self.assertFalse((self.root / 'attempts').exists())

    def test_actual_native_boundary_error_keeps_pipeline_unknown_fence(self):
        pipeline = self.pipeline(opt_in=True, post_boundary=True)
        reply = pipeline.apply({'id': 'native-after-boundary', 'tool': 'pipeline_run',
                                'arguments': {'stage': 'E', 'driver': 'launch'}})
        self.assertEqual(reply['status'], 'failed')
        self.assertEqual(reply['data']['result']['launch_outcome'], 'unknown')
        run = Path(pipeline.config['state']) / reply['data']['run']
        self.assertTrue((run / 'driver/dispatch-boundary.json').is_file())
        self.assertIn('ContractMismatch', (run / 'driver/stderr.log').read_text())
        self.assertEqual(len(Pipeline(pipeline.config).pending_launches()), 1)
        self.assertNotIn('launch_rejections', pipeline.state)

    def test_prepare_pins_helper_and_opts_in_only_native_launch_for_all_modes(self):
        for mode in ('max', 'min', 'matmul'):
            output = self.root / ('prepared-' + mode)
            runtime = self.root / ('runtime-' + mode + '.json')
            runtime.write_text(json.dumps({'config': {'providers': {'fixture': {'model': 'unchanged'}}},
                                           'application': {'max_model_turns': 123}}))
            task = profile = None
            if mode == 'matmul':
                task = self.root / 'cube-task.json'; task.write_text(json.dumps({
                    'goal': 'implement declared Cube fixture', 'contract': self.config['contract']}))
                profile = self.root / 'cube-profile.json'; profile.write_text(json.dumps({'mode': mode, 'device': 1,
                    'transport_module': self.config['transport_module'], 'remote_root': '/no-dispatch',
                    'oracle_files': {'oracle.py': str(self.oracle)}, 'baseline_files': {'oracle.py': str(self.oracle)}}))
            prepare(SimpleNamespace(output=output, task=task, profile=profile, runtime_profile=runtime,
                mode=mode, device=1, transport=Path(self.config['transport_module']), remote_root='/no-dispatch',
                python=Path(sys.executable)))
            config = json.loads((output / 'pipeline-tools/config.json').read_text())
            for name, driver in config['drivers'].items():
                self.assertEqual(driver.get('preflight_rejection_protocol'),
                                 preflight.PROTOCOL if name == 'npu-launch' else None)
                helper = ROOT / 'examples/operator_pipeline/preflight.py'
                self.assertEqual(driver['files'][str(helper)], adapter.sha(helper))
            copied = output / 'pipeline-tools/preflight.py'
            self.assertEqual(copied.read_bytes(), helper.read_bytes())
            tools = json.loads((output / 'business-tools.json').read_text())
            self.assertEqual(len(tools), 11)
            for tool in tools: self.assertIn({'path': str(copied), 'sha256': adapter.sha(copied)}, tool['files'])
            fresh = json.loads((output / 'profile.json').read_text())
            self.assertEqual(fresh['application']['max_model_turns'], 20)
            self.assertEqual(fresh['config']['providers'], {'fixture': {'model': 'unchanged'}})
            self.assertFalse((output / 'project/candidate.py').exists())
            self.assertFalse(any((output / 'attempts').iterdir()))
            self.assertFalse((output / 'operator-pipeline').exists())


if __name__ == '__main__': unittest.main()
