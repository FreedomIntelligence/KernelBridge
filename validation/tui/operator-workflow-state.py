"""State guidance boundaries; no model or device dispatch."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'examples/operator_pipeline'))
from pipeline import Pipeline, main


class WorkflowStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'candidate.py'; self.source.write_text('x = 2\n')
        self.oracle = self.root / 'oracle.py'; self.oracle.write_text('# independent checker\n')
        self.config = {'state': str(self.root / 'state'), 'read_roots': [str(self.root)], 'drivers': {}}

    def tearDown(self): self.temp.cleanup()

    def call(self, tool, **arguments):
        result = main(self.config, {'id': uuid.uuid4().hex, 'tool': tool, 'arguments': arguments})
        self.assertEqual(result['status'], 'completed', result)
        return result['data']

    def freeze(self):
        self.call('pipeline_bind', contract={'task': 'fixture', 'oracle': {'kind': 'independent', 'description': 'No torch required',
                  'files': {'oracle.py': str(self.oracle)}}, 'suite': [{'id': 'one'}], 'measurement': {'scope': 'protocol_fixture'}})
        self.call('pipeline_plan', stage='B', record={'family': 'one'})
        self.call('pipeline_plan', stage='C', record={'hypothesis': 'fixture implementation'})
        return self.call('pipeline_freeze', files={'candidate.py': str(self.source)}, origin={'scope': 'fixture'})

    def test_missing_driver_is_explicit_and_status_never_executes(self):
        action = self.freeze()['workflow']['next_actions'][0]
        self.assertEqual(action['stage'], 'F'); self.assertIsNone(action['tool'])
        self.assertEqual(action['status'], 'unavailable')
        before = len(Pipeline(self.config).state['receipts'])
        workflow = Pipeline(self.config).workflow()
        self.assertEqual(len(Pipeline(self.config).state['receipts']), before)
        self.assertIn('unavailable', workflow['calibration'])

    def test_no_torch_bootstrap_has_explicit_self_iteration_comparator(self):
        driver = ROOT / 'validation/tui/operator-workflow-driver.py'
        self.config['drivers']['check'] = {'kind': 'check', 'argv': [sys.executable, '-B', str(driver), 'check'],
            'files': {str(driver): hashlib.sha256(driver.read_bytes()).hexdigest()}}
        self.freeze(); self.call('pipeline_run', stage='F', driver='check')
        accepted = self.call('pipeline_promote')
        self.assertTrue(accepted['result']['bootstrap']); self.assertIsNone(accepted['result']['speedup'])
        self.assertEqual(accepted['workflow']['self_iteration']['arguments'], {'selection': 'incumbent'})
        archived = self.call('pipeline_archive', knowledge={'scope': 'local protocol', 'official': False})
        self.assertEqual(archived['workflow']['next_actions'][0]['stage'], 'B')
        self.call('pipeline_baseline', selection='incumbent')
        self.assertIsNone(Pipeline(self.config).state['check'])
        self.assertEqual(Pipeline(self.config).state['baseline'], Pipeline(self.config).state['incumbent'])

    def test_missing_required_diagnostic_does_not_recommend_repeating_promotion(self):
        driver = ROOT / 'validation/tui/operator-workflow-driver.py'
        self.config['drivers']['check'] = {'kind': 'check', 'argv': [sys.executable, '-B', str(driver), 'check'],
            'files': {str(driver): hashlib.sha256(driver.read_bytes()).hexdigest()}}
        self.config['diagnostics'] = {'required': True, 'scope': 'protocol_fixture', 'drivers': {}}
        self.freeze()
        result = self.call('pipeline_run', stage='F', driver='check')
        action = result['workflow']['next_actions'][0]
        self.assertEqual(action['stage'], 'J'); self.assertIsNone(action['tool'])
        self.assertEqual(action['status'], 'blocked')

    def test_new_candidate_is_archived_after_calibration_despite_old_archive(self):
        driver = ROOT / 'validation/tui/operator-workflow-driver.py'
        for kind in ['check', 'calibrate']:
            self.config['drivers'][kind] = {'kind': kind, 'argv': [sys.executable, '-B', str(driver), kind],
                'files': {str(driver): hashlib.sha256(driver.read_bytes()).hexdigest()}}
        self.freeze(); self.call('pipeline_run', stage='F', driver='check'); self.call('pipeline_promote')
        self.call('pipeline_run', stage='I', driver='calibrate'); self.call('pipeline_archive', knowledge={'variant': 2})
        self.call('pipeline_plan', stage='B', record={'family': 'one'})
        self.call('pipeline_plan', stage='C', record={'hypothesis': 'next implementation'})
        self.source.write_text('x = 3\n')
        self.call('pipeline_freeze', files={'candidate.py': str(self.source)}, origin={'scope': 'fixture'})
        self.call('pipeline_run', stage='F', driver='check'); self.call('pipeline_promote')
        result = self.call('pipeline_run', stage='I', driver='calibrate')
        self.assertEqual(result['workflow']['next_actions'][0]['tool'], 'pipeline_archive')

    def test_baseline_change_reuses_original_completed_source_handle(self):
        driver = ROOT / 'validation/tui/operator-workflow-driver.py'
        for kind in ['launch', 'collect', 'check']:
            self.config['drivers'][kind] = {'kind': kind, 'argv': [sys.executable, '-B', str(driver), kind],
                'files': {str(driver): hashlib.sha256(driver.read_bytes()).hexdigest()}}
        self.freeze()
        handle = self.call('pipeline_run', stage='E', driver='launch')['result']['result']['handle']
        self.call('pipeline_run', stage='E', driver='collect', handle=handle)
        result = self.call('pipeline_baseline', selection='files', files={'candidate.py': str(self.source)})
        action = result['workflow']['next_actions'][0]
        self.assertEqual(action['stage'], 'F'); self.assertEqual(action['arguments']['handle'], handle)
        self.assertEqual(len(Pipeline(self.config).state['handles']), 1)

    def test_inline_contents_are_rejected_with_path_contract(self):
        self.freeze()
        result = main(self.config, {'id': uuid.uuid4().hex, 'tool': 'pipeline_freeze',
                      'arguments': {'files': {'candidate.py': 'x = 2\n'}, 'origin': {'scope': 'fixture'}}})
        self.assertEqual(result['status'], 'failed')
        self.assertIn('source file paths, not file contents', result['data']['result']['error'])


if __name__ == '__main__': unittest.main()
