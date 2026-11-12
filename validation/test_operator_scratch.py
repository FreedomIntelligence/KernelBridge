"""Local workspace/checkpoint boundaries; no model or device execution."""
import base64
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for path in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src']:
    sys.path.insert(0, str(path))

from kernelcat_executor.contracts.models import Action, Conflict, Policy, digest
from kernelcat_executor.capabilities.workspace_policy import WorkspaceGuard, filesystem_case_sensitive
from kernelcat_worker.application_artifacts import (
    persist, read_source_snapshot, restore_source, source_snapshot,
)


class ScratchCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kb-scratch-')
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'workspace'
        self.root.mkdir()
        (self.root / 'refs').mkdir()
        (self.root / 'refs/oracle.py').write_text('immutable oracle')
        (self.root / 'AGENTS.md').write_text('immutable instructions')
        self.readonly = ('refs', 'AGENTS.md', '.kernelbridge/agents')

    def tearDown(self):
        self.temp.cleanup()

    def capture(self):
        return source_snapshot(self.root, None, self.readonly)

    def restore(self, value):
        restore_source(self.root, value, None, self.readonly)

    @staticmethod
    def record(path, content):
        encoded = {'text': content.decode('utf-8')} if content.isascii() else {'base64': base64.b64encode(content).decode()}
        return {'path': path, **encoded, 'sha256': hashlib.sha256(content).hexdigest(), 'mode': 0o644}

    @staticmethod
    def snapshot(files):
        return {'files': files, 'source_sha256': digest(files)}

    def test_utf8_identity_and_binary_modes_roundtrip_without_touching_references(self):
        text = '\ufeff# 中文\nprint(1)\n'
        candidate = self.root / 'candidate.py'
        candidate.write_text(text); candidate.chmod(0o755)
        old_files = [{'path': 'candidate.py', 'text': text,
                      'sha256': hashlib.sha256(text.encode()).hexdigest(), 'mode': 0o755}]
        old = self.capture()
        self.assertEqual(old, self.snapshot(old_files), 'legacy text records and digest remain identical')
        binary = self.root / '__pycache__/helper.pyc'
        binary.parent.mkdir(); binary.write_bytes(b'\xff\x80\0\r\n'); binary.chmod(0o640)
        frozen = self.capture()
        self.assertIn('base64', frozen['files'][0])
        candidate.write_text('changed'); binary.write_bytes(b'changed')
        (self.root / 'extra.txt').write_text('remove on rollback')
        self.restore(frozen)
        self.assertEqual(self.capture(), frozen)
        self.assertEqual(binary.read_bytes(), b'\xff\x80\0\r\n')
        self.assertEqual(binary.stat().st_mode & 0o777, 0o640)
        self.assertFalse((self.root / 'extra.txt').exists())
        self.restore(old)
        self.assertEqual(self.capture(), old)
        self.assertFalse(binary.exists())
        self.assertEqual((self.root / 'refs/oracle.py').read_text(), 'immutable oracle')
        self.assertEqual((self.root / 'AGENTS.md').read_text(), 'immutable instructions')

    def test_file_directory_shape_changes_and_repeated_restore(self):
        scratch = self.root / 'scratch'
        scratch.write_text('checkpoint file')
        file_snapshot = self.capture()
        scratch.unlink(); scratch.mkdir()
        (scratch / 'nested.bin').write_bytes(b'\xff\0')
        directory_snapshot = self.capture()
        self.restore(file_snapshot)
        self.assertEqual(scratch.read_text(), 'checkpoint file')
        self.restore(file_snapshot)
        self.restore(directory_snapshot)
        self.assertEqual((scratch / 'nested.bin').read_bytes(), b'\xff\0')
        self.restore(directory_snapshot)
        self.assertEqual(self.capture(), directory_snapshot)

    def test_interrupted_restore_can_repeat_after_shape_change(self):
        (self.root / 'a.py').write_text('desired candidate')
        (self.root / 'scratch').mkdir()
        (self.root / 'scratch/helper.py').write_text('desired helper')
        frozen = self.capture()
        (self.root / 'a.py').write_text('current candidate')
        (self.root / 'scratch/helper.py').unlink(); (self.root / 'scratch').rmdir()
        (self.root / 'scratch').write_text('obstructing file')
        original_replace = os.replace
        calls = 0

        def interrupted(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('injected interruption during second install')
            return original_replace(source, target)

        with patch('kernelcat_worker.application_artifacts.os.replace', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'injected interruption'):
                self.restore(frozen)
        self.restore(frozen)
        self.assertEqual(self.capture(), frozen)

    def test_restore_preflights_every_target_before_modifying_any_file(self):
        (self.root / 'candidate.py').write_text('current candidate')
        (self.root / 'scratch').mkdir(); (self.root / 'scratch/private').write_text('outside allowlist')
        before = (self.root / 'candidate.py').read_bytes()
        unsafe = self.snapshot([self.record('candidate.py', b'changed'), self.record('refs/oracle.py', b'bad')])
        with self.assertRaises(Conflict):
            self.restore(unsafe)
        self.assertEqual((self.root / 'candidate.py').read_bytes(), before)
        shape = self.snapshot([self.record('candidate.py', b'changed'), self.record('scratch', b'file')])
        with self.assertRaises(Conflict):
            restore_source(self.root, shape, ('candidate.py', 'scratch'), self.readonly)
        self.assertEqual((self.root / 'candidate.py').read_bytes(), before)
        self.assertEqual((self.root / 'scratch/private').read_text(), 'outside allowlist')

    def test_snapshot_tampering_and_unsafe_paths_fail_before_restore(self):
        (self.root / 'candidate.py').write_text('current candidate')
        good = self.record('candidate.py', b'desired')
        cases = [
            [{**good, 'sha256': '0' * 64}],
            [{**good, 'base64': 'AA=='}],
            [{key: value for key, value in good.items() if key != 'text'} | {'base64': '!!bad!!'}],
            [good, good],
            [{**good, 'path': '../escape.py'}],
            [{**good, 'path': '.git/config'}],
            [{**good, 'path': 'nested/../candidate.py'}],
            [{**good, 'mode': True}],
        ]
        for files in cases:
            with self.subTest(files=files):
                with self.assertRaises(Conflict): self.restore(self.snapshot(files))
                self.assertEqual((self.root / 'candidate.py').read_text(), 'current candidate')
        bad = self.snapshot([good]); bad['source_sha256'] = '0' * 64
        with self.assertRaises(Conflict): self.restore(bad)
        (self.root / 'link').symlink_to(self.base)
        with self.assertRaises(Conflict): self.capture()

    def test_encoded_checkpoints_larger_than_default_artifact_limit_reload_and_repersist(self):
        for index in range(60):
            (self.root / f'cache-{index:02}.bin').write_bytes(b'\xff' * 262144)
        frozen = self.capture()
        path = self.base / 'checkpoint.json'
        persist(path, frozen)
        self.assertGreater(path.stat().st_size, 16 * 1024 * 1024)
        persist(path, frozen)
        self.assertEqual(read_source_snapshot(path, frozen['source_sha256']), frozen)
        (self.root / 'too-large.bin').write_bytes(b'x' * 262145)
        with self.assertRaises(Conflict): self.capture()

    def test_project_conversation_marker_remains_readable_but_not_restorable_as_source(self):
        marker = {'source_sha256': digest({'snapshot_kind': 'conversation_only'}),
                  'snapshot_kind': 'conversation_only', 'files': []}
        path = self.base / 'marker.json'; persist(path, marker)
        self.assertEqual(read_source_snapshot(path, marker['source_sha256']), marker)
        with self.assertRaises(Conflict): self.restore(marker)

    def test_guard_allows_helpers_but_preserves_restricted_and_readonly_boundaries(self):
        broad = WorkspaceGuard(self.root, None, self.readonly)
        restricted = WorkspaceGuard(self.root, ('candidate.py',), self.readonly)

        def verdict(guard, name):
            return guard.check(Action(id='write', task='task', tool='write', arguments={'path': name}), 'test', Policy()).verdict

        self.assertEqual(verdict(broad, 'parts_head.txt'), 'abstain')
        self.assertEqual(verdict(restricted, 'parts_head.txt'), 'deny')
        for name in ('refs/oracle.py', 'refs/new.py', 'AGENTS.md', '.kernelbridge', '.kernelbridge/agents/role.md', '.git/config', '../escape'):
            self.assertEqual(verdict(broad, name), 'deny', name)
        if not filesystem_case_sensitive(self.root):
            for name in ('REFS/new.py', 'agents.md', '.KERNELBRIDGE/AGENTS/role.md', '.GIT/config'):
                self.assertEqual(verdict(broad, name), 'deny', name)
            (self.root / 'helper.py').write_text('canonical')
            self.assertEqual(verdict(broad, 'HELPER.py'), 'deny')
        else:
            self.assertEqual(verdict(broad, 'REFS/new.py'), 'abstain')

    def test_case_alias_restore_and_destination_collisions_are_rejected(self):
        if filesystem_case_sensitive(self.root):
            self.skipTest('requires an actually case-insensitive filesystem')
        (self.root / 'candidate.py').write_text('current candidate')
        for path in ('agents.md', 'REFS/new.py', '.KERNELBRIDGE/AGENTS/role.md', '.GIT/config'):
            with self.subTest(path=path):
                frozen = self.snapshot([self.record('candidate.py', b'changed'), self.record(path, b'bad')])
                with self.assertRaises(Conflict): self.restore(frozen)
                self.assertEqual((self.root / 'candidate.py').read_text(), 'current candidate')
        collision = self.snapshot([self.record('helper.py', b'one'), self.record('Helper.py', b'two')])
        with self.assertRaises(Conflict): self.restore(collision)
        self.assertFalse((self.root / 'helper.py').exists())
        self.assertEqual((self.root / 'AGENTS.md').read_text(), 'immutable instructions')


if __name__ == '__main__':
    unittest.main()
