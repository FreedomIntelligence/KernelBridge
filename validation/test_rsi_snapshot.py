"""A final draft must not replace an already retained candidate."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import test_runtime as support
from kernelcat_executor.contracts.models import Conflict
from kernelcat_worker.rsi_retained import retained_bundle


class RsiSnapshotTests(unittest.TestCase):
    setUp = support.RuntimeTests.setUp
    tearDown = support.RuntimeTests.tearDown

    def bundle(self, source, *, missing=False):
        if not missing:
            (self.workspace.root / 'solution.py').write_text('unmeasured draft\n')
        original = {'schema_version': 'kernelcat.program-bundle/v1',
                    'entrypoint': 'solution.py', 'configuration_source': '{}',
                    'files': [{'path': 'solution.py', 'text': 'initial\n'}]}
        records = SimpleNamespace(baseline=lambda: {'value': {'source': source}})
        return retained_bundle(self.workspace, original, records, ['solution.py'])

    def test_snapshot_uses_retained_source_and_preserves_draft_and_reference(self):
        result = self.bundle([{'path': 'solution.py', 'text': 'accepted\n'}])
        files = {item['path']: item['text'] for item in result['files']}
        self.assertEqual(files['solution.py'], 'accepted\n')
        self.assertEqual(files['reference/spec.txt'], 'immutable')
        self.assertEqual((self.workspace.root / 'solution.py').read_text(), 'unmeasured draft\n')

    def test_missing_retained_file_is_rejected(self):
        with self.assertRaises(Conflict):
            self.bundle([])

    def test_deleted_draft_entrypoint_is_recovered_from_retained_source(self):
        result = self.bundle([{'path': 'solution.py', 'text': 'accepted\n'}], missing=True)
        self.assertEqual(next(x['text'] for x in result['files'] if x['path'] == 'solution.py'), 'accepted\n')
        self.assertFalse((self.workspace.root / 'solution.py').exists())

    def test_retained_source_cannot_replace_reference(self):
        with self.assertRaises(Conflict):
            self.bundle([{'path': 'reference/spec.txt', 'text': 'changed'}])
