"""Actual Pipeline.apply/native adapter -> host E3, using no-device fixtures.

Synthetic timings exercise protocol checks only. The inherited fake transport
permits original artifact reads and rejects every launch/measurement script.
"""
import copy
import hashlib
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import uuid

import test_npu_diagnostic_projection as projection
from pipeline import Pipeline

ROOT = Path(__file__).resolve().parents[1]
for path in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src']:
    sys.path.insert(0, str(path))
from kernelcat_executor.contracts.models import digest
from kernelcat_worker.application_pipeline_acceptance import collect_pipeline_acceptance, promoted_receipt


class DiagnosticAcceptanceEnvelopeTests(projection.OriginalNpuDiagnosticFixture):
    def setUp(self):
        super().setUp()
        self.actions = {}
        measurement = self.contract['measurement']
        measurement.update(require_noise=True, protocol_id='local-native-projection-fixture-v1', scope='same_device_npu_eager_abba')
        self.config['contract']['measurement'] = copy.deepcopy(measurement)
        suite = [{'id': 'fast', 'family': 'fast'}, {'id': 'slow', 'family': 'slow'}]
        self.contract['suite'] = suite
        self.config['contract']['suite'] = copy.deepcopy(suite)
        cases = []; null_cases = []
        for spec, candidate_us in zip(suite, (50., 101.), strict=True):
            input_sha = hashlib.sha256(spec['id'].encode()).hexdigest()
            arms = [{'pair': block, 'position': position, 'arm': 'eager' if position in (0, 3) else 'candidate',
                     'input_sha256': input_sha, 'us': 100. if position in (0, 3) else candidate_us}
                    for block in range(3) for position in range(4)]
            cases.append({'id': spec['id'], 'input_sha256': input_sha, 'correct': True,
                          'candidate': candidate_us, 'eager': 100., 'speedup': 100. / candidate_us, 'arms': arms})
            null_cases.append({'id': spec['id'], 'input_sha256': input_sha,
                               'arms': [{**arm, 'arm': 'eager', 'us': 100.} for arm in arms]})
        self.result.update(status='completed', correct=True, profile=True, cases=cases,
                           geomean_speedup=math.sqrt(2. * 100. / 101.))
        noise = {'schema_version': 'kernelbridge.noise-calibration/v1', 'protocol': 'independent_eager_aaaa',
                 **{key: self.result[key] for key in ('pairs', 'warmup', 'repeats', 'device', 'baseline_sha256', 'driver_sha256', 'source_sha256', 'environment')},
                 'cases': null_cases}
        self.result['noise_calibration'] = noise
        self.result['noise_calibration_sha256'] = hashlib.sha256((json.dumps(noise, indent=2) + '\n').encode()).hexdigest()
        self.unavailable_ir()

    def unavailable_ir(self):
        self.result['diagnostics'] = {'triggered': True, 'reason': 'required', 'required': True, 'satisfied': False,
            'reports': [{'kind': 'ir', 'status': 'unavailable', 'scope': 'npu_diagnostic',
                         'candidate_sha256': self.source['files']['candidate.py'], 'reason': 'Fixture compiler cannot emit IR'},
                        self.artifact('profiler')]}
        self.seal_result()

    def call(self, pipeline, tool, **arguments):
        identity = uuid.uuid4().hex
        action = {'id': identity, 'task': 'fixture-task', 'tool': tool, 'arguments': arguments}
        outcome = pipeline.apply(action)
        self.actions[identity] = {'action': action, 'terminal': True, 'outcome': copy.deepcopy(outcome)}
        fresh = Pipeline(pipeline.config); pipeline.chain = fresh.chain; pipeline.state = fresh.state
        return identity, outcome

    def evaluate(self, *, required=False, large=False):
        if large:
            self.result['environment']['retained_context'] = 'original environment context ' * 1000
            self.result['noise_calibration']['environment'] = self.result['environment']
            self.result['noise_calibration_sha256'] = hashlib.sha256((json.dumps(self.result['noise_calibration'], indent=2) + '\n').encode()).hexdigest()
            self.seal_result()
        pipeline = self.pipeline({'required': required})
        if large:
            wrapper = self.root / 'original-check-with-context.py'
            wrapper.write_text('import json,sys\n'
                + 'sys.path.insert(0,' + repr(str(ROOT / 'examples/scatter')) + ')\n'
                + 'from npu_pipeline_driver import execute\n'
                + 'from pathlib import Path\n'
                + 'result=execute(json.loads(Path(sys.argv[1]).read_text()),"check",json.load(sys.stdin))\n'
                + 'result["original_evaluator_context"]="retained original context "*1000\n'
                + 'print(json.dumps(result))\n')
            driver = pipeline.config['drivers']['npu-check']
            driver['argv'] = [sys.executable, '-B', str(wrapper), str(self.root / 'original-adapter.json')]
            driver['files'][str(wrapper)] = projection.adapter.sha(wrapper)
        fid, f = self.call(pipeline, 'pipeline_run', stage='F', driver='npu-check')
        self.assertEqual(f['status'], 'completed', f)
        gid, g = self.call(pipeline, 'pipeline_pair', driver='npu-measure')
        self.assertEqual(g['status'], 'completed', g)
        pair = pipeline.state['pair']
        self.assertTrue(pair['qualification']['qualified'])
        self.assertGreater(pair['qualification']['paired_delta'], pair['qualification']['noise_threshold'])
        self.assertEqual(pair['regressions'], ['slow'])
        self.assertEqual(g['data']['diagnostics']['reports'][0]['status'], self.result['diagnostics']['reports'][0]['status'])
        hid, h = self.call(pipeline, 'pipeline_promote')
        return pipeline, fid, gid, hid, h

    def host_for(self, pipeline, hid):
        candidate = (self.folder / 'candidate.py').read_text()
        files = [{'path': 'candidate.py', 'sha256': self.source['files']['candidate.py'], 'text': candidate, 'mode': 0o644}]
        source_sha = digest(files)
        snapshots = self.root / 'host/checkpoints'; snapshots.mkdir(parents=True, exist_ok=True)
        (snapshots / (source_sha + '.json')).write_text(json.dumps({'source_sha256': source_sha, 'files': files}))
        contract = copy.deepcopy(self.config['contract'])
        contract['oracle']['files'] = {name: projection.adapter.sha(path) for name, path in contract['oracle']['files'].items()}
        self.declared = {'kind': 'operator_pipeline', 'tool': 'pipeline_promote', 'evidence_tier': 'E3',
                         'pipeline_contract': contract, 'baseline_files': self.baseline['files'],
                         'source_paths': ['candidate.py'], 'scope': 'same_device_npu_eager_abba'}
        self.contexts = {key: {'task_version': 1, 'evaluator_sha256': 'fixture-evaluator', 'source_sha256': source_sha} for key in self.actions}
        self.host_state = {'task_version': 1, 'evidence': [], 'goal': {'status': 'active'}}
        self.commands = []
        events = [{'kind': 'admitted', 'action': row['action'], 'application_context': self.contexts[key]} for key, row in self.actions.items()]
        store = SimpleNamespace(path=self.root / 'host/events.jsonl', events=lambda: events,
                                _events=lambda values: values, snapshot=lambda: {'actions': self.actions})
        self.host = SimpleNamespace(task=SimpleNamespace(id='fixture-task'), owner=SimpleNamespace(gateway=SimpleNamespace(store=store)),
            evaluator={'sha256': 'fixture-evaluator', 'files': {}}, config=SimpleNamespace(application=SimpleNamespace(complete_on_acceptance=True)),
            journal=SimpleNamespace(command=lambda name, value: self.commands.append((name, value))))
        self.hid = hid

    def accepted(self):
        return promoted_receipt(self.host, self.host_state, self.declared, self.hid,
                                self.actions[self.hid], self.actions, self.contexts)

    def check_projection(self, identity):
        return self.actions[identity]['outcome']['data']['result']

    def assert_original_projection(self, identity, expected):
        data = self.actions[identity]['outcome']['data']
        run = self.root / 'workflow' / data['run']
        disk = json.loads((run / 'result.json').read_text())
        self.assertEqual(disk['data'], expected)
        if isinstance(data['result'], dict): self.assertEqual(data['result'], expected)
        if 'diagnostics' in data:
            self.assertEqual(disk['diagnostics'], data['diagnostics'])
            self.assertNotIn('diagnostics', disk['data'])
        manifest = json.loads((run / 'manifest.json').read_text())
        self.assertEqual(manifest['result.json'], projection.adapter.sha(run / 'result.json'))
        self.assertEqual(data['manifest_sha256'], projection.adapter.sha(run / 'manifest.json'))

    def test_optional_unavailable_IR_accepts_actual_positive_pair_and_completes_goal(self):
        pipeline, fid, gid, hid, h = self.evaluate()
        self.assertEqual(h['status'], 'completed', h)
        self.assertFalse(pipeline.config['diagnostics']['required'])
        self.assertTrue(self.result['diagnostics']['required'])
        self.assertFalse(self.result['diagnostics']['satisfied'])
        self.assert_original_projection(fid, json.loads(Path(pipeline.state['check']['receipt']).read_text()))
        self.assert_original_projection(gid, pipeline.state['pair'])
        self.host_for(pipeline, hid)
        self.assertIsNotNone(self.accepted())
        collect_pipeline_acceptance(self.host, self.host_state, self.declared)
        self.assertEqual([name for name, _ in self.commands], ['evidence', 'evidence', 'goal_outcome'])
        self.assertEqual([data['evidence_tier'] for name, data in self.commands if name == 'evidence'], ['E1', 'E3'])
        self.assertEqual(self.commands[-1][1]['status'], 'completed')
        self.assertEqual([row['path'] for row in self.reads()], ['profiler.csv'])

    def test_explicit_required_true_still_blocks_H_when_IR_is_unavailable(self):
        pipeline, fid, gid, hid, h = self.evaluate(required=True)
        self.assertEqual(h['status'], 'failed')
        self.assertIn('Required source-bound ir', h['data']['result']['error'])
        self.assertIsNone(pipeline.state['incumbent'])
        self.assertTrue(pipeline.state['check']['passed'])
        self.assertTrue(pipeline.state['pair']['qualification']['qualified'])

    def test_required_complete_F_and_G_envelopes_preserve_strict_host_acceptance(self):
        self.result['diagnostics'].update(satisfied=True, reports=[self.artifact('ir'), self.artifact('profiler')])
        self.seal_result()
        pipeline, fid, gid, hid, h = self.evaluate(required=True)
        self.assertEqual(h['status'], 'completed', h)
        self.assertIn('diagnostics', self.actions[fid]['outcome']['data'])
        self.assert_original_projection(fid, json.loads(Path(pipeline.state['check']['receipt']).read_text()))
        self.assert_original_projection(gid, pipeline.state['pair'])
        self.host_for(pipeline, hid); self.assertIsNotNone(self.accepted())
        self.assertEqual(len(self.reads()), 2)  # F downloaded; G reused, never launched.

    def test_large_F_G_results_use_original_action_bound_hash_fallback(self):
        self.result['diagnostics'].update(satisfied=True, reports=[self.artifact('ir'), self.artifact('profiler')])
        self.seal_result()
        pipeline, fid, gid, hid, h = self.evaluate(required=True, large=True)
        self.assertEqual(h['status'], 'completed', h)
        for identity in (fid, gid):
            self.assertIsInstance(self.actions[identity]['outcome']['data']['result'], str)
            self.assertIn('diagnostics', self.actions[identity]['outcome']['data'])
        self.assert_original_projection(fid, json.loads(Path(pipeline.state['check']['receipt']).read_text()))
        self.assert_original_projection(gid, pipeline.state['pair'])
        self.host_for(pipeline, hid); self.assertIsNotNone(self.accepted())
        path = self.root / 'workflow' / self.actions[gid]['outcome']['data']['run'] / 'result.json'
        value = json.loads(path.read_text()); value['diagnostics']['reports'][0]['status'] = 'unavailable'
        path.write_text(json.dumps(value))  # Even separate feedback remains manifest-bound.
        self.assertIsNone(self.accepted())

    def test_independent_envelope_does_not_relax_any_original_receipt_comparison(self):
        pipeline, fid, gid, hid, h = self.evaluate()
        self.assertEqual(h['status'], 'completed', h)
        self.host_for(pipeline, hid); self.assertIsNotNone(self.accepted())
        saved = copy.deepcopy(self.actions)
        mutations = [
            lambda: self.check_projection(fid)['result']['checks'].update(full=False),
            lambda: self.actions[gid]['outcome']['data']['result']['cases'].update(fast=999.),
            lambda: self.actions[gid]['outcome']['data']['result'].update(diagnostics={'attempt_to_hide_raw_difference': True}),
        ]
        for mutate in mutations:
            self.actions = copy.deepcopy(saved); mutate(); self.assertIsNone(self.accepted())
        self.actions = saved
        path = Path(pipeline.state['check']['receipt']); original = path.read_bytes(); path.write_bytes(original + b'\n')
        self.assertIsNone(self.accepted()); path.write_bytes(original)
        self.contexts[gid]['source_sha256'] = 'different-original-source'; self.assertIsNone(self.accepted())


if __name__ == '__main__': unittest.main()
