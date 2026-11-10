"""Import a real historical Pi conversation into a fresh Gateway and execute another turn."""
from pathlib import Path
import hashlib
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import test_runtime as runtime_support


class PortableReplayTests(unittest.TestCase):
    setUp = runtime_support.RuntimeTests.setUp
    tearDown = runtime_support.RuntimeTests.tearDown
    session = runtime_support.RuntimeTests.session
    def test_pi_loads_explicit_agents_into_actual_model_context(self):
        text = (runtime_support.ROOT / 'examples/cann/instructions/AGENTS.md').read_text()
        (self.workspace.root / 'AGENTS.md').write_text(text)
        (self.workspace.root / 'AGENTS.task.md').write_text('Task appendix identity: PORTABLE_CANN_CONTEXT_2026')
        session, provider, _ = self.session([('write', {'path': 'AGENTS.md', 'content': 'tampered'})])
        self.assertEqual(session.run(self.task)['status'], 'finished')
        model_context = json.dumps(provider.harnesses, ensure_ascii=False)
        self.assertIn('PORTABLE_CANN_CONTEXT_2026', model_context)
        self.assertIn('先寻找能消去的工作', model_context)
        self.assertEqual((self.workspace.root / 'AGENTS.md').read_text(), text)

    def test_portable_import_executes_with_rebound_tools(self):
        original, _, replay = self.session([('write', {'path': 'solution.py', 'content': 'original = 7\n'})])
        self.assertEqual(original.run(self.task)['status'], 'finished')
        source = next((replay.directory / 'sessions').glob('*.jsonl'))
        original_bytes = source.read_bytes()
        imported = self.base / 'archive.jsonl'; imported.write_bytes(original_bytes)
        old_cwd = str(self.workspace.root)
        from kernelcat_executor.capabilities.filesystem import Workspace
        from kernelcat_executor.profiles.store import AgentJournal
        from kernelcat_executor.contracts.models import Policy
        self.base = self.base / 'different host with spaces'; self.base.mkdir()
        self.workspace = Workspace(self.base / 'workspace')
        (self.workspace.root / 'reference').mkdir()
        (self.workspace.root / 'reference/spec.txt').write_text('immutable')
        (self.workspace.root / 'solution.py').write_text('original = 7\n')
        self.store = AgentJournal(self.base / 'state/events.jsonl')
        self.store.initialize(300, Policy(max_steps=25, context_bytes=262144))
        self.task = self.task.model_copy(update={'id': 'continued-test'})
        session, provider, restored = self.session([
            ('edit', {'path': 'solution.py', 'edits': [{'oldText': 'original = 7', 'newText': 'continued = 8'}]}),
            ('evaluate', {}),
            ('write', {'path': 'reference/spec.txt', 'content': 'should be denied'}),
        ])
        restored.seed = {'path': str(imported), 'sha256': hashlib.sha256(original_bytes).hexdigest()}
        self.assertEqual(session.run(self.task)['status'], 'finished')
        self.assertEqual((self.workspace.root / 'solution.py').read_text(), 'continued = 8\n')
        self.assertEqual((self.workspace.root / 'reference/spec.txt').read_text(), 'immutable')
        self.assertEqual(imported.read_bytes(), original_bytes)
        latest = max((restored.directory / 'sessions').glob('*.jsonl'), key=lambda p: p.stat().st_mtime)
        rows = [json.loads(line) for line in latest.read_text().splitlines()]
        self.assertEqual(rows[0]['id'], json.loads(original_bytes.splitlines()[0])['id'])
        self.assertEqual(rows[0]['cwd'], str(self.workspace.root))
        self.assertNotEqual(rows[0]['cwd'], old_cwd)
        self.assertIn('original = 7', latest.read_text())
        self.assertIn('continued = 8', latest.read_text())
        self.assertTrue(any('evaluate' in declarations for declarations in provider.declarations))
        restored._boundary()
