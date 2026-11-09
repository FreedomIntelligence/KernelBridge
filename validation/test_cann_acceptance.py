"""The CANN example must reject success text without evidence for the final bytes."""
import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'source/packages/executor/src'))
from kernelcat_executor.contracts.models import Task, digest

spec = importlib.util.spec_from_file_location('cann_acceptance', ROOT / 'examples/cann/validate.py')
acceptance = importlib.util.module_from_spec(spec); spec.loader.exec_module(acceptance)


class CannAcceptance(unittest.TestCase):
    def test_finished_message_and_stale_receipt_cannot_qualify_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = Task.model_validate_json((ROOT / 'examples/cann/vector-add/task.json').read_text())
            workspace = root / 'workspace' / digest(task)[:20]
            workspace.mkdir(parents=True)
            candidate = workspace / 'solution.py'; candidate.write_text('original bytes')
            (root / 'task.json').write_text(task.model_dump_json())
            (root / 'config.json').write_text(json.dumps({'workspace': str(root / 'workspace'), 'state_dir': str(root / 'state')}))
            (root / 'result.json').write_text(json.dumps({'status': 'finished', 'result': {'summary': 'all tests passed'}}))
            with patch.object(acceptance, 'AgentJournal') as journal:
                journal.return_value.snapshot.return_value = {'actions': {}}
                with self.assertRaisesRegex(ValueError, 'no original native success receipt'):
                    acceptance.verify(root)
                outcome = {'status': 'completed', 'data': {
                    'candidate_sha256': hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    'reference_sha256': hashlib.sha256(task.seed_files['reference/reference.py'].encode()).hexdigest(),
                    'result': {'compiled': True, 'correctness': True, 'failure_stage': None,
                        'hardware': task.contract['target']['soc'], 'correctness_policy': {'atol': 1e-4, 'rtol': 1e-4},
                        'build': {'files': [{'path': 'fixture.so', 'sha256': 'f' * 64}]},
                        'performance': {'timer': 'npu_event', 'num_trials': 100, 'samples_ms': [0.1] * 100}}}}
                journal.return_value.snapshot.return_value = {'actions': {'original': {
                    'action': {'task': task.id, 'tool': 'evaluate'}, 'terminal': True, 'outcome': outcome}}}
                self.assertTrue(acceptance.verify(root)['accepted'])
                candidate.write_text('changed after successful evaluation')
                with self.assertRaisesRegex(ValueError, 'no original native success receipt'):
                    acceptance.verify(root)

    def test_unresolved_effect_blocks_success_even_after_finished_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = Task.model_validate_json((ROOT / 'examples/cann/vector-add/task.json').read_text())
            workspace = root / 'workspace' / digest(task)[:20]; workspace.mkdir(parents=True)
            (workspace / 'solution.py').write_text('source')
            (root / 'task.json').write_text(task.model_dump_json())
            (root / 'config.json').write_text(json.dumps({'workspace': str(root / 'workspace'), 'state_dir': str(root / 'state')}))
            (root / 'result.json').write_text('{"status":"finished"}')
            with patch.object(acceptance, 'AgentJournal') as journal:
                journal.return_value.snapshot.return_value = {'actions': {'unknown': {
                    'action': {'task': task.id, 'tool': 'evaluate'}, 'terminal': False}}}
                with self.assertRaisesRegex(ValueError, 'unresolved'):
                    acceptance.verify(root)
