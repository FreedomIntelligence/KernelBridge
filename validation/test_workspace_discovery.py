"""Bounded child file discovery shares the original workspace path policy."""
import tempfile
from pathlib import Path
import unittest

import test_runtime
from kernelcat_executor.capabilities.filesystem import Workspace
from kernelcat_executor.contracts.models import Action, ToolFailure


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Workspace(Path(self.temp.name))
        root = self.workspace.root
        (root / 'src').mkdir()
        (root / 'src/operator.py').write_text('def scatter_max():\n    return "中文 contract"\n')
        (root / 'src/second.py').write_text('scatter_min = "CONTRACT"\n')
        (root / '.git').mkdir(); (root / '.git/private').write_text('contract')
        (root / 'alias').symlink_to(root / 'src', target_is_directory=True)
        (root / 'linked.py').symlink_to(root / 'src/operator.py')

    def tearDown(self): self.temp.cleanup()

    def call(self, name, **arguments):
        return getattr(self.workspace, name)(Action(id='discovery', task='fixture', tool=name, arguments=arguments)).data

    def test_files_search_and_truncation(self):
        self.assertEqual(self.call('list_files')['paths'], ['src/operator.py', 'src/second.py'])
        self.assertTrue(self.call('list_files', limit=1)['truncated'])
        hits = self.call('search_text', query='contract')['matches']
        self.assertEqual([(h['path'], h['line']) for h in hits], [('src/operator.py', 2), ('src/second.py', 1)])
        self.assertEqual(len(self.call('search_text', query='CONTRACT', ignore_case=False)['matches']), 1)
        self.assertTrue(self.call('search_text', query='contract', limit=1)['truncated'])
        self.assertEqual(self.call('search_text', query='scatter.*')['matches'], [])
        self.assertEqual(len(self.call('search_text', path='src/operator.py', query='中文')['matches']), 1)

    def test_escape_reserved_and_symlinks_rejected(self):
        for path in ('../', str(self.workspace.root), '.git', 'alias', 'linked.py'):
            with self.subTest(path=path):
                with self.assertRaises(ToolFailure): self.call('list_files', path=path)
                with self.assertRaises(ToolFailure): self.call('search_text', path=path, query='contract')

    def test_binary_and_large_files_reported_as_skipped(self):
        (self.workspace.root / 'binary.dat').write_bytes(b'\xff\x00')
        (self.workspace.root / 'large.txt').write_bytes(b'c' * (self.workspace.max_bytes + 1))
        data = self.call('search_text', query='contract')
        self.assertEqual(data['skipped_files'], 2)
        self.assertEqual(data['scanned_files'], 2)


if __name__ == '__main__': unittest.main(verbosity=2)
