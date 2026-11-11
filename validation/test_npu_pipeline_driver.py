"""Clean workspace and exact original-receipt projection boundaries, without NPU access."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/scatter'))
import npu_pipeline_driver as adapter
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
        summary = {'status': 'completed', 'correct': True, 'remote_result_sha256': raw_sha,
                   'original_handle': {'pid': 42}, 'remote_attempt': '/remote/original'}
        observation = {'result': self.result, 'result_sha256': raw_sha, 'cpu_fallback': False, 'log_tail': ''}
        for name, value in [('intent.json', intent), ('device-result.json', self.result), ('result.json', summary), ('observation.json', observation)]:
            (self.folder / name).write_text(json.dumps(value))
        self.request = {'source': self.source, 'binding': {'source': self.source['sha256'], 'baseline': self.baseline['sha256']},
                        'contract': self.contract, 'handle': {'pid': 42}, 'role': 'F', 'output': str(self.root / 'check')}

    def test_four_arm_projection_uses_every_original_sample_without_new_measurement(self):
        checked = adapter.execute(self.config, 'check', self.request)
        self.assertEqual(checked['checks'], dict(build=True, semantics=True, full=True))
        for index in range(4):
            request = dict(self.request, source=self.baseline if index in (0, 3) else self.source,
                           role='ABBA'[index], output=str(self.root / f'arm-{index}'))
            result = adapter.execute(self.config, 'measure', request)
            self.assertEqual(result['cases']['case-one'], [self.result['cases'][0]['arms'][4 * pair + index]['us'] for pair in range(3)])
            self.assertEqual(result['new_measurements'], 0)

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
        self.assertNotIn('AGENTS.md', task['files'])
        self.assertEqual(len(specs), 10)
        self.assertEqual([s['name'] for s in specs if s.get('capture_paths')], ['pipeline_freeze'])
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
