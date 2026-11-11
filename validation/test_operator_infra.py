"""Read-only reuse of the existing ops infra and local source preparation."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import unittest
import uuid
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('KERNELBRIDGE_INFRA_SOURCE', '/Users/jerry/ops/infra'))


@unittest.skipUnless(SOURCE.is_dir(), 'explicit local ops infra unavailable')
class InfraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        spec = importlib.util.spec_from_file_location('infra_prepare', ROOT / 'examples/operator_infra/prepare.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.driver = self.root / 'driver.py'; self.driver.write_text('print("local driver completed")\n')
        self.tools = module.prepare({'source_root': str(SOURCE), 'read_roots': [str(SOURCE.parent), str(self.root)],
            'drivers': {'local': {'cwd': str(self.root), 'argv': [sys.executable, '-B', str(self.driver)], 'inputs': [str(self.driver)]}}}, self.root / 'adapter', sys.executable)
        self.before = {name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() for name in ('runs.json', 'knowledge.json', 'forge_goal.json') if (SOURCE / name).exists()}

    def tearDown(self):
        self.assertEqual(self.before, {name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() for name in self.before})
        self.temp.cleanup()

    def call(self, name, failed=False, **arguments):
        tool = next(t for t in self.tools if t['name'] == name)
        result = subprocess.run(tool['argv'], input=json.dumps({'id': uuid.uuid4().hex, 'tool': name, 'arguments': arguments}),
            capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome['status'], 'failed' if failed else 'completed', outcome)
        return outcome['data']

    def test_context_design_preflight_and_original_output(self):
        context = self.call('infra_context', operator='Unique')
        self.assertTrue(context['files'])
        self.call('infra_design', operator='Unique')
        preflight = self.call('infra_preflight', card={'operator': 'Unique'})
        self.assertFalse(preflight['summary']['ready'])
        page = self.call('infra_read', run=context['run'], path='result.json', limit=256)
        self.assertLessEqual(len(page['text'].encode()), 260)

    def test_local_release_and_campaign_parent_identity(self):
        archive = self.root / 'source.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('build.sh', '#!/bin/sh\ntrue\n')
            bundle.writestr('kernel.cpp', '// ordinary comment\nint run(int x) { return x + 1; }\n')
        campaign = self.call('infra_campaign_init', archive=str(archive), operator='ScatterMax')
        self.assertEqual(campaign['summary']['parent_zip_sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertFalse(campaign['summary']['device_acceptance'])
        prepared = self.call('infra_release_prepare', archive=str(archive))
        folder = self.root / 'operator-infra' / prepared['run']; cleaned = self.root / 'clean.zip'
        with zipfile.ZipFile(cleaned, 'w') as bundle:
            for path in (folder / 'release').rglob('*'):
                if path.is_file(): bundle.write(path, str(path.relative_to(folder / 'release')))
        bound = self.call('infra_release_bind', manifest=str(folder / 'release-manifest.json'), archive=str(cleaned))
        self.assertEqual(bound['summary']['rebuilt_zip_sha256'], hashlib.sha256(cleaned.read_bytes()).hexdigest())

    def test_trajectory_ir_and_no_remote_initializer(self):
        source = self.root / 'kernel.cpp'; source.write_text('int run(int x) { return x + 1; }\n')
        trajectory = self.call('infra_trajectory', paths=[str(source)])
        self.assertEqual(trajectory['summary']['files'][0]['sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
        a = self.root / 'a.ll'; b = self.root / 'b.ll'
        a.write_text('define i32 @run(i32 %x) {\n  ret i32 %x\n}\n')
        b.write_text('define i32 @run(i32 %x) {\n  %r = add i32 %x, 1\n  ret i32 %r\n}\n')
        ir = self.call('infra_ir_render', paths=[str(a), str(b)])
        self.assertEqual(ir['summary']['pc_mapping'], 'unavailable')
        self.assertFalse((self.root / 'adapter/start_engram_forge.py').exists())
        self.assertFalse(any('gateway' in t['name'] for t in self.tools))

    def test_reject_outside_roots_bad_archive_and_unconfigured_driver(self):
        self.call('infra_trajectory', paths=['/etc/hosts'], failed=True)
        self.call('infra_run_driver', driver='remote-upload', failed=True)
        self.call('infra_cannbench_query', path='//other.example', failed=True)
        archive = self.root / 'bad.zip'
        with zipfile.ZipFile(archive, 'w') as bundle: bundle.writestr('../escape', 'bad')
        self.call('infra_campaign_init', archive=str(archive), operator='bad', failed=True)
        self.call('infra_release_prepare', archive=str(archive), failed=True)
        self.assertFalse((self.root / 'escape').exists())

    def test_index_compare_and_pinned_driver(self):
        campaign = self.root / 'reports'; campaign.mkdir()
        for label, elapsed in [('a', 10), ('b', 9)]:
            folder = campaign / 'official' / label; folder.mkdir(parents=True)
            (folder / 'report.json').write_text(json.dumps({'operators': [{'operator': 'ScatterMax',
                'cases': [{'case_id': 'one', 'status': 'success', 'elapsed_us': elapsed, 'baseline_perf_us': 20, 't_hw_us': 1}]}]}))
        environment = self.root / 'environment.json'; environment.write_text('{"scope":"local_fixture"}')
        experiments = self.root / 'experiments.json'; experiments.write_text('{}')
        index = self.call('infra_index', root=str(campaign), operator='ScatterMax', environment=str(environment), experiments=str(experiments))
        self.assertEqual(index['summary']['indexed'], 2)
        compared = self.call('infra_compare', baseline='ScatterMax/a', candidate='ScatterMax/b')
        self.assertFalse(compared['summary']['promotion_evidence_sufficient'])
        self.assertAlmostEqual(compared['summary']['cases'][0]['change_pct'], -10)
        receipt = self.call('infra_run_driver', driver='local')['summary']
        self.assertEqual(receipt['phase'], 'exited'); self.assertEqual(receipt['correctness'], 'not_interpreted')
        self.driver.write_text('print("changed")\n')
        self.call('infra_run_driver', driver='local', failed=True)

    def test_trace_clean_pc_fidelity_and_changed_artifact_rejected(self):
        raw = {'traceEvents': [{'name': 'VADD', 'ph': 'X', 'pid': 'veccore0', 'tid': 'VECTOR',
            'ts': 2, 'dur': 3, 'args': {'pc_addr': '0x20', 'detail': 'fixture'}}]}
        data = json.dumps(raw).encode(); capture = self.root / 'trace.bin'
        capture.write_bytes(struct.pack('<QBBBB', len(data), 2, 0, 0, 0x5A) + data)
        cleaned = self.call('infra_trace_clean', path=str(capture))
        directory = cleaned['summary']['output']
        pc = self.call('infra_trace_pc', directory=directory, core='veccore0', pc='0x20')['summary']
        self.assertEqual(pc['raw_events'], raw['traceEvents'])
        self.assertEqual(pc['capabilities']['pc_to_cpp'], 'unavailable')
        (Path(directory) / 'trace.raw.json').write_text('{}')
        self.call('infra_trace_pc', directory=directory, core='veccore0', pc='0x20', failed=True)

    def test_forge_audit_uses_upstream_dag_parser(self):
        campaign = self.root / 'campaign'; (campaign / 'task').mkdir(parents=True)
        (campaign / 'task/cases.yaml').write_text(json.dumps({'cases': [{'case_id': i} for i in range(20)]}))
        (campaign / 'task/golden.py').write_text('# protocol fixture, not an oracle\n')
        (campaign / 'milestone.md').write_text('# Empty test milestone\n')
        result = self.call('infra_forge_audit', campaign=str(campaign))['summary']
        self.assertEqual(result['runs'], [])
        self.assertFalse(result['promotion_evidence_sufficient'])

    def test_cannbench_adapter_get_only_without_network(self):
        spec = importlib.util.spec_from_file_location('infra_adapter', ROOT / 'examples/operator_infra/adapter.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        from types import SimpleNamespace
        requests = []
        api = SimpleNamespace(request=lambda path, **kw: requests.append((path, kw)) or {'scope': 'protocol_fixture'})
        config = json.loads((self.root / 'adapter/config.json').read_text())
        with patch.object(module, 'load', return_value=api), patch.dict(os.environ, {'CANNBENCH_TOKEN': 'fixture-only'}):
            module.run({'tool': 'infra_cannbench_query', 'arguments': {'path': '/api/tasks'}}, config, self.root)
        self.assertEqual(requests, [('/api/tasks', {'method': 'GET'})])

    @unittest.skipUnless((ROOT / '.validation/scatter-deps/torch').is_dir(), 'optional upstream framework dependencies unavailable')
    def test_dq_score_replays_original_framework_formula(self):
        framework = SOURCE.parent / '实验/Unique_ForgeKernel_120/environment/cann-bench-d9cb127b40f2c454c8df32cc29c65d0852c1f608'
        if not framework.is_dir(): self.skipTest('retained upstream framework unavailable')
        (self.root / 'framework').symlink_to(framework, target_is_directory=True)
        folder = self.root / 'official/fixture'; folder.mkdir(parents=True)
        report = folder / 'report.json'
        # Synthetic timing inputs exercise the original scoring code, not a
        # leaderboard observation or a new measurement.
        report.write_text(json.dumps({'tasks_version': '2.0.0', 'perf_metric': 'eq3', 'score_weight_profile': '2',
            'overall_score': 65.0, 'operators': [{'operator': 'DynamicQuant', 'cases': [
                {'case_id': 'fixture_' + str(i), 'status': 'success', 'elapsed_us': 20,
                 'baseline_perf_us': 20, 't_hw_us': 1} for i in range(20)]}]}))
        with patch.dict(os.environ, {'PYTHONPATH': str(ROOT / '.validation/scatter-deps')}):
            result = self.call('infra_score_opportunities', report=str(report))['summary']
        self.assertEqual(result['base_score'], 65)
        self.assertEqual(len(result['ranking']), 20)
        self.assertEqual(result['report_sha256'], hashlib.sha256(report.read_bytes()).hexdigest())
        self.assertIn('not measured speedups', result['scope'])


if __name__ == '__main__': unittest.main()
