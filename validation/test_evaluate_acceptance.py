"""Actual composite D/E/F -> G -> H -> host E3 with original native receipts.

All device results and timings are synthetic protocol fixtures; the transport
accepts only the existing original-artifact read script and never dispatches.
"""
import json
from pathlib import Path
import sys
import hashlib
import uuid
from unittest.mock import patch

import test_diagnostic_acceptance_envelope as envelope
import test_npu_diagnostic_projection as projection
from pipeline import Pipeline

ROOT = Path(__file__).resolve().parents[1]


class EvaluateAcceptanceTests(envelope.DiagnosticAcceptanceEnvelopeTests):
    def pipeline(self, switches=None):
        pipeline = projection.OriginalNpuDiagnosticFixture.pipeline(self, switches, frozen=False, retained=False)
        pipeline.config['evaluation_enabled'] = True
        config_path = self.root / 'original-adapter.json'
        # E projects a known completed original job. This fixture never launches
        # a device, while exercising the actual adapter and terminal feedback.
        for name, kind in [('npu-launch', 'launch'), ('npu-collect', 'collect')]:
            pipeline.config['drivers'][name] = {'kind': kind, 'argv': [sys.executable, '-B',
                str(ROOT / 'examples/scatter/npu_pipeline_driver.py'), str(config_path), 'collect'],
                'files': {str(path): projection.adapter.sha(path) for path in
                    (config_path, self.transport, ROOT / 'examples/scatter/npu_pipeline_driver.py')}}
        return pipeline

    def call(self, pipeline, tool, **arguments):
        if tool == 'pipeline_run' and arguments.get('stage') == 'F':
            tool = 'pipeline_evaluate'
            arguments = {'files': {'candidate.py': str(self.folder / 'candidate.py')},
                         'origin': {'scope': 'original local receipt fixture'}, 'check_driver': arguments['driver']}
        return super().call(pipeline, tool, **arguments)

    def check_projection(self, identity):
        return self.actions[identity]['outcome']['data']['result']['check_receipt']

    def assert_original_projection(self, identity, expected):
        if self.actions[identity]['action']['tool'] != 'pipeline_evaluate':
            return super().assert_original_projection(identity, expected)
        data = self.actions[identity]['outcome']['data']
        run = self.root / 'workflow' / data['run']
        disk = json.loads((run / 'result.json').read_text())
        body = disk['data']
        self.assertEqual(body['check_receipt'], expected)
        self.assertEqual([row['stage'] for row in body['stages']], ['D', 'E', 'F'])
        self.assertEqual(body['check_action_id'], identity)
        if isinstance(data['result'], dict): self.assertEqual(data['result']['check_receipt'], expected)
        if 'diagnostics' in data:
            self.assertEqual(disk['diagnostics'], data['diagnostics'])
            self.assertNotIn('diagnostics', disk['data']['check_receipt'])
        phase = body['stages'][-1]
        self.assertEqual(phase['receipt']['sha256'], projection.adapter.sha(phase['receipt']['path']))
        manifest = json.loads((run / 'manifest.json').read_text())
        self.assertEqual(manifest['result.json'], projection.adapter.sha(run / 'result.json'))
        self.assertEqual(data['manifest_sha256'], projection.adapter.sha(run / 'manifest.json'))

    def test_actual_orphan_F_recovery_reaches_host_E3_without_reexecuting_F(self):
        pipeline = self.pipeline()
        identity = uuid.uuid4().hex
        action = {'id': identity, 'task': 'fixture-task', 'tool': 'pipeline_evaluate', 'arguments': {
            'files': {'candidate.py': str(self.folder / 'candidate.py')}, 'origin': {'scope': 'original fixture'}}}
        finish = Pipeline.finish
        def crash_outer(instance, current, *args, **kwargs):
            if current['tool'] == 'pipeline_evaluate': raise KeyboardInterrupt('outer crashed after F commit')
            return finish(instance, current, *args, **kwargs)
        with patch.object(Pipeline, 'finish', crash_outer), self.assertRaises(KeyboardInterrupt):
            pipeline.apply(action)
        self.actions[identity] = {'action': action, 'terminal': False, 'outcome': {'status': 'unknown'}}
        pipeline = Pipeline(pipeline.config)
        check_path = Path(pipeline.state['check']['receipt']); before = check_path.read_bytes()
        fid, recovered = self.call(pipeline, 'pipeline_evaluate', resume_run=hashlib.sha256(identity.encode()).hexdigest())
        self.assertEqual(recovered['status'], 'completed', recovered)
        self.assertEqual(recovered['data']['result']['stages'][0]['origin_action_id'], identity)
        self.assertEqual(check_path.read_bytes(), before)
        gid, g = self.call(pipeline, 'pipeline_pair', driver='npu-measure')
        self.assertEqual(g['status'], 'completed', g)
        hid, h = self.call(pipeline, 'pipeline_promote')
        self.assertEqual(h['status'], 'completed', h)
        self.host_for(pipeline, hid)
        accepted = self.accepted(); self.assertIsNotNone(accepted)
        self.assertEqual(accepted['original_f'], fid)
        self.assertEqual(pipeline.state['check']['receipt'], str(check_path))
        self.assertEqual(len([row for row in pipeline.state['receipts'] if row['tool'] == 'pipeline_run'
            and (Path(pipeline.config['state']) / row['run'] / 'driver/receipt.json').exists()
            and json.loads((Path(pipeline.config['state']) / row['run'] / 'driver/receipt.json').read_text())['kind'] == 'check']), 1)
