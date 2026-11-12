"""Clean workspace and exact original-receipt projection boundaries, without NPU access."""
import copy
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/scatter'))
import npu_pipeline_driver as adapter
import npu_trial as native_trial
from prepare_npu_campaign import prepare


class NpuPipelineDriverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.oracle = self.root / 'oracle'; self.oracle.mkdir()
        for name in ('npu_benchmark.py', 'npu_eager.py'): (self.oracle / name).write_text('# independent test fixture\n')
        self.folder = self.root / 'attempts/attempt-0001'; self.folder.mkdir(parents=True)
        (self.folder / 'candidate.py').write_text('kernel_src = "test fixture"\n')
        source_sha = adapter.sha(self.folder / 'candidate.py')
        self.source = {'root': str(self.folder), 'files': {'candidate.py': source_sha}}
        self.source['sha256'] = adapter.manifest_sha(self.source['files'])
        self.baseline = {'root': str(self.oracle), 'files': {'npu_eager.py': adapter.sha(self.oracle / 'npu_eager.py')}}
        self.baseline['sha256'] = adapter.manifest_sha(self.baseline['files'])
        oracle = {'root': str(self.oracle), 'files': {p.name: adapter.sha(p) for p in self.oracle.iterdir()}}
        oracle['sha256'] = adapter.manifest_sha(oracle['files'])
        self.contract = {'task': {'outputs': 'unchanged exact semantics'}, 'suite': [{'id': 'case-one'}],
                         'oracle': {'kind': 'independent', 'description': 'test oracle', 'files': oracle},
                         'measurement': {'device': 0, 'warmup': 3, 'repeats': 10, 'pairs': 3, 'aggregation': 'pooled_arm_median'}}
        frozen = copy.deepcopy(self.contract); frozen['oracle']['files'] = {p.name: str(p) for p in self.oracle.iterdir()}
        self.config = {'state': str(self.folder.parent), 'contract': frozen}
        arms = [{'pair': pair, 'arm': name, 'us': 100 + pair * 10 + i}
                for pair in range(3) for i, name in enumerate(('eager', 'candidate', 'candidate', 'eager'))]
        self.result = {'status': 'completed', 'correct': True, 'source_sha256': source_sha,
                       'driver_sha256': oracle['files']['npu_benchmark.py'], 'baseline_sha256': oracle['files']['npu_eager.py'],
                       'device': 0, 'warmup': 3, 'repeats': 10, 'pairs': 3, 'soc': 'test-soc', 'environment': {'device': 'test'},
                       'geomean_speedup': 1.0, 'cases': [{'id': 'case-one', 'input_sha256': 'fixed-input', 'correct': True,
                       'candidate': 111.5, 'eager': 111.5, 'speedup': 1.0, 'arms': arms}]}
        raw_sha = hashlib.sha256((json.dumps(self.result, indent=2, allow_nan=False) + '\n').encode()).hexdigest()
        intent = {'attempt': 1, 'source_sha256': source_sha, 'action_id': 'real-tool-id'}
        self.handle = json.loads((ROOT / 'validation/fixtures/npu-job-handle.json').read_text())['handle']
        summary = {'status': 'completed', 'correct': True, 'remote_result_sha256': raw_sha,
                   'original_handle': self.handle, 'remote_attempt': '/remote/original'}
        observation = {'result': self.result, 'result_sha256': raw_sha, 'cpu_fallback': False, 'log_tail': ''}
        for name, value in [('intent.json', intent), ('device-result.json', self.result), ('result.json', summary), ('observation.json', observation)]:
            (self.folder / name).write_text(json.dumps(value))
        self.request = {'source': self.source, 'binding': {'source': self.source['sha256'], 'baseline': self.baseline['sha256']},
                        'contract': self.contract, 'handle': adapter.wire_handle(self.handle), 'role': 'F', 'output': str(self.root / 'check')}

    def test_original_handle_collect_cli_is_lossless_for_all_shared_operator_modes(self):
        before = {path: path.read_bytes() for path in self.folder.iterdir()}
        for mode in ('max', 'min', 'matmul'):
            with self.subTest(mode=mode):
                config = self.root / 'config.json'; config.write_text(json.dumps(dict(self.config, mode=mode)))
                output = subprocess.check_output([sys.executable, '-B', str(ROOT / 'examples/scatter/npu_pipeline_driver.py'),
                    str(config), 'collect'], input=json.dumps(self.request).encode())
                wire = subprocess.check_output(['node', '-e',
                    'process.stdout.write(JSON.stringify(JSON.parse(require("node:fs").readFileSync(0,"utf8"))))'], input=output)
                result = json.loads(wire)
                self.assertEqual(json.loads(output), result)
                self.assertEqual(result['handle']['started_ns'], '1794401504504640402')
                self.assertEqual(result['original_handle'], result['handle'])
                self.assertTrue(result['read_only_projection'])
        self.assertEqual({path: path.read_bytes() for path in self.folder.iterdir()}, before)

    def test_legacy_handle_reads_match_exactly_and_rounded_handles_are_rejected(self):
        before = {path: path.read_bytes() for path in self.folder.iterdir()}
        with patch.object(adapter, 'trial', side_effect=AssertionError('must not redispatch')), \
                patch.object(adapter, 'transport', side_effect=AssertionError('must not contact remote')):
            for handle in (self.handle, adapter.wire_handle(self.handle)):
                result = adapter.execute(self.config, 'collect', dict(self.request, handle=handle))
                self.assertEqual(result['handle'], adapter.wire_handle(self.handle))
            for stamp in (1794401504504640500, '1794401504504640500', self.handle['started_ns'] + 1):
                with self.subTest(stamp=stamp), self.assertRaisesRegex(ValueError, 'differs from the original job'):
                    adapter.execute(self.config, 'check', dict(self.request, handle=dict(self.handle, started_ns=stamp)))
        self.assertEqual({path: path.read_bytes() for path in self.folder.iterdir()}, before)

    def test_four_arm_projection_uses_every_original_sample_without_new_measurement(self):
        checked = adapter.execute(self.config, 'check', self.request)
        self.assertEqual(checked['checks'], dict(build=True, semantics=True, full=True))
        self.assertEqual(checked['original_handle'], adapter.wire_handle(self.handle))
        for index in range(4):
            request = dict(self.request, source=self.baseline if index in (0, 3) else self.source,
                           role='ABBA'[index], output=str(self.root / f'arm-{index}'))
            result = adapter.execute(self.config, 'measure', request)
            self.assertEqual(result['cases']['case-one'], [self.result['cases'][0]['arms'][4 * pair + index]['us'] for pair in range(3)])
            self.assertEqual(result['new_measurements'], 0)
            self.assertEqual(result['original_handle'], adapter.wire_handle(self.handle))
        for kind in ('ir', 'profiler'):
            result = adapter.execute(self.config, kind, self.request)
            self.assertEqual(result['original_handle'], adapter.wire_handle(self.handle))
            self.assertEqual(result['status'], 'unavailable')

    def launch_fixture(self):
        pipeline = self.root / 'pipeline'; pipeline.mkdir()
        (pipeline / 'event-00000001.json').write_text(json.dumps({'body': {'C': {'hypothesis': 'original'}}}))
        project = self.root / 'project'; project.mkdir()
        transport = self.root / 'transport.py'
        transport.write_text('def run(*a, **kw): raise AssertionError("never redispatch")\n')
        config = dict(self.config, candidate=str(project / 'candidate.py'), pipeline_state=str(pipeline),
                      transport_module=str(transport))
        request = dict(self.request, action_id='real-tool-id', handle=None, role='E')
        return config, request

    def test_cached_original_launch_projects_handle_without_dispatch(self):
        # Keep the historical one-field fixture: recovery projects an existing
        # receipt; it must not re-admit the source under today's parser.
        before = {path: path.read_bytes() for path in self.folder.iterdir()}
        config, request = self.launch_fixture()
        with patch.object(adapter, 'sources', side_effect=AssertionError('historical source reparsed')) as parsed, \
             patch.object(native_trial, 'sources', side_effect=AssertionError('historical source reparsed')) as trial_parsed, \
             patch.object(native_trial, 'transport', side_effect=AssertionError('cached transport imported')) as transport, \
             patch.object(native_trial, 'invoke_remote', side_effect=AssertionError('cached remote called')) as remote:
            result = adapter.execute(config, 'launch', request)
        for mocked in (parsed, trial_parsed, transport, remote): mocked.assert_not_called()
        self.assertEqual(result['handle'], adapter.wire_handle(self.handle))
        self.assertEqual(result['original_handle'], result['handle'])
        self.assertEqual({path: path.read_bytes() for path in self.folder.iterdir()}, before)

    def test_pending_original_launch_only_collects_original_hash_and_handle(self):
        config, request = self.launch_fixture()
        original = {path.name: path.read_bytes() for path in self.folder.iterdir()}
        terminal_names = ('device-result.json', 'observation.json', 'result.json')
        for name in terminal_names: (self.folder / name).unlink()
        transport = object()

        def finish_original(collected_config, folder, run):
            self.assertEqual(folder, self.folder)
            self.assertEqual(run, transport)
            self.assertEqual((folder / 'intent.json').read_bytes(), original['intent.json'])
            self.assertEqual((folder / 'candidate.py').read_bytes(), original['candidate.py'])
            for name in terminal_names: (folder / name).write_bytes(original[name])
            return {'status': 'completed', 'data': native_trial.wire_summary(json.loads(original['result.json']))}

        with patch.object(adapter, 'sources', side_effect=AssertionError('historical source reparsed')), \
             patch.object(native_trial, 'sources', side_effect=AssertionError('historical source reparsed')), \
             patch.object(native_trial, 'transport', return_value=transport) as opened, \
             patch.object(native_trial, 'invoke_remote', side_effect=AssertionError('must only collect')) as remote, \
             patch.object(native_trial, 'collect', side_effect=finish_original) as collected:
            result = adapter.execute(config, 'launch', request)
        opened.assert_called_once(); collected.assert_called_once(); remote.assert_not_called()
        self.assertEqual(result['handle'], adapter.wire_handle(self.handle))
        self.assertEqual(result['native_candidate_sha256'], json.loads(original['intent.json'])['source_sha256'])
        self.assertEqual(result['remote_result_sha256'], json.loads(original['result.json'])['remote_result_sha256'])
        self.assertEqual({path.name: path.read_bytes() for path in self.folder.iterdir()}, original)
        self.assertFalse((self.folder.parent / 'attempt-0002').exists())

    def test_unresolved_original_launch_never_becomes_not_started(self):
        config, request = self.launch_fixture()
        (self.folder / 'result.json').unlink()
        before = {path.name: path.read_bytes() for path in self.folder.iterdir()}
        with patch.object(adapter, 'sources', side_effect=AssertionError('historical source reparsed')), \
             patch.object(native_trial, 'sources', side_effect=AssertionError('historical source reparsed')), \
             patch.object(native_trial, 'transport', return_value=object()) as opened, \
             patch.object(native_trial, 'invoke_remote', side_effect=AssertionError('must only collect')) as remote, \
             patch.object(native_trial, 'collect', return_value={'status': 'unknown', 'data': {'error': 'original pending'}}) as collected, \
             patch.object(adapter, 'make_source_rejection', side_effect=AssertionError('cannot claim not_started')) as rejected, \
             self.assertRaisesRegex(RuntimeError, 'unresolved'):
            adapter.execute(config, 'launch', request)
        opened.assert_called_once(); collected.assert_called_once(); remote.assert_not_called(); rejected.assert_not_called()
        self.assertEqual({path.name: path.read_bytes() for path in self.folder.iterdir()}, before)
        self.assertFalse((Path(request['output']) / 'preflight').exists())
        self.assertFalse((self.folder.parent / 'attempt-0002').exists())

    def test_existing_action_identity_conflicts_never_become_preflight_rejections(self):
        config, original_request = self.launch_fixture()
        original = {path.name: path.read_bytes() for path in self.folder.iterdir()}
        frozen = self.root / 'independent-frozen'; frozen.mkdir()
        candidate = frozen / 'candidate.py'
        duplicate = self.folder.parent / 'attempt-0002'
        for mutation in ('new_source', 'archived_source', 'duplicate_action', 'binding', 'linked_intent', 'contract'):
            with self.subTest(mutation=mutation):
                for name, raw in original.items():
                    path = self.folder / name
                    if path.is_symlink(): path.unlink()
                    path.write_bytes(raw)
                if duplicate.exists():
                    for path in duplicate.iterdir(): path.unlink()
                    duplicate.rmdir()
                candidate.write_bytes(original['candidate.py'])
                request = copy.deepcopy(original_request)
                request['source']['root'] = str(frozen)
                if mutation == 'new_source':
                    candidate.write_text('kernel_src = "different source, still invalid for new admission"\n')
                    request['source']['files']['candidate.py'] = adapter.sha(candidate)
                    request['source']['sha256'] = adapter.manifest_sha(request['source']['files'])
                    request['binding']['source'] = request['source']['sha256']
                elif mutation == 'archived_source':
                    (self.folder / 'candidate.py').write_text('archived bytes were changed\n')
                elif mutation == 'duplicate_action':
                    duplicate.mkdir()
                    for name in ('intent.json', 'candidate.py'): (duplicate / name).write_bytes(original[name])
                elif mutation == 'binding':
                    request['binding']['source'] = '0' * 64
                elif mutation == 'linked_intent':
                    saved = self.root / 'linked-original-intent.json'
                    saved.write_bytes(original['intent.json'])
                    (self.folder / 'intent.json').unlink()
                    (self.folder / 'intent.json').symlink_to(saved)
                else:
                    request['contract']['task'] = {'outputs': 'changed contract'}
                before = {str(path.relative_to(self.folder.parent)): path.read_bytes()
                          for path in self.folder.parent.rglob('*') if path.is_file()}
                with ExitStack() as stack:
                    forbidden = []
                    for module, name in ((adapter, 'trial'), (adapter, 'sources'), (adapter, 'transport'),
                                         (adapter, 'make_rejection'), (adapter, 'make_source_rejection'),
                                         (native_trial, 'transport'), (native_trial, 'invoke_remote')):
                        forbidden.append(stack.enter_context(patch.object(module, name,
                            side_effect=AssertionError('identity conflict must not call ' + name))))
                    message = {'duplicate_action': 'Ambiguous original action identity',
                               'linked_intent': 'Original attempt identity contains a link',
                               'contract': 'differs from the frozen task'}.get(
                                   mutation, 'Original action source identity changed')
                    with self.assertRaisesRegex(ValueError, message):
                        adapter.execute(config, 'launch', request)
                for mocked in forbidden: mocked.assert_not_called()
                self.assertEqual({str(path.relative_to(self.folder.parent)): path.read_bytes()
                                  for path in self.folder.parent.rglob('*') if path.is_file()}, before)
                self.assertFalse((Path(request['output']) / 'preflight').exists())
                self.assertFalse((Path(request['output']) / 'dispatch-boundary.json').exists())

    def test_changed_semantics_suite_or_measurement_is_rejected(self):
        for field, replacement in [('task', {'outputs': 'weakened'}), ('suite', []), ('measurement', dict(self.contract['measurement'], aggregation='changed'))]:
            request = copy.deepcopy(self.request); request['contract'][field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'differs from the frozen task'):
                adapter.execute(self.config, 'check', request)

    def test_local_timing_edit_cannot_borrow_original_receipt_hash(self):
        changed = copy.deepcopy(self.result); changed['cases'][0]['arms'][0]['us'] = 99999
        (self.folder / 'device-result.json').write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'differs from the original device receipt'):
            adapter.execute(self.config, 'measure', self.request)

    def test_prepare_is_clean_and_does_not_expose_source_directory_as_read_root(self):
        transport = self.root / 'transport.py'; transport.write_text('def run(*a, **kw): raise AssertionError("never dispatch")\n')
        output = self.root / 'clean'
        args = SimpleNamespace(output=output, task=None, profile=None, runtime_profile=None, mode='max', device=0,
                               transport=transport, remote_root='/remote/task', python=Path(sys.executable))
        prepared = prepare(args)
        task = json.loads((output / 'task.json').read_text()); specs = json.loads((output / 'business-tools.json').read_text())
        self.assertEqual(task['schema_version'], 'kernelbridge.operator-task/v1')
        self.assertFalse((output / 'project/candidate.py').exists())
        self.assertFalse((output / 'initial.py').exists())
        self.assertEqual(task['files']['AGENTS.md'], (ROOT / 'examples/cann/instructions/AGENTS.md').read_text())
        self.assertIn('AGENTS.md', task['readonly'])
        self.assertIn('AGENTS.task.md', task['readonly'])
        self.assertEqual(len(specs), 11)
        self.assertEqual([s['name'] for s in specs if s.get('capture_paths')], ['pipeline_freeze', 'pipeline_evaluate'])
        evaluate = next(s for s in specs if s['name'] == 'pipeline_evaluate')
        self.assertEqual(evaluate['capture_paths'], ['candidate.py'])
        helper = output / 'pipeline-tools/evaluation.py'
        self.assertIn({'path': str(helper), 'sha256': adapter.sha(helper)}, evaluate['files'])
        config = json.loads((output / 'pipeline-tools/config.json').read_text())
        self.assertNotIn(str(ROOT / 'examples/scatter'), config['read_roots'])
        sys.path.insert(0, str(output / 'pipeline-tools'))
        previous_cwd = Path.cwd(); os.chdir(output / 'project')
        try:
            spec = importlib.util.spec_from_file_location('test_pipeline_path', output / 'pipeline-tools/pipeline.py')
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            pipeline = module.Pipeline(config)
            with self.assertRaisesRegex(ValueError, 'outside configured read roots'):
                pipeline.path(str(ROOT / 'examples/scatter/ascend_sources.py'))
            with self.assertRaisesRegex(ValueError, 'outside configured read roots'):
                pipeline.path(str(ROOT / 'examples/cann/scatter-max/baseline.py'))
            self.assertEqual(pipeline.path(str(ROOT / 'examples/scatter/npu_eager.py')), ROOT / 'examples/scatter/npu_eager.py')
        finally:
            os.chdir(previous_cwd); sys.path.pop(0)


if __name__ == '__main__': unittest.main()
