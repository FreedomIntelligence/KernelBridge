"""Authoring inputs must remain separate from trusted runtime and native wrapper contents."""
import ast
import importlib.util
import json
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/cann'))
from task_bundle import load_bundle
from pack_native import pack


class CannDelivery(unittest.TestCase):
    def test_native_sources_roundtrip_without_changing_cpp(self):
        for name, wrapper in [('vector-add', 'baseline.py'), ('cube-matmul', 'baseline.py'), ('sigmoid-optimization', 'solution.py')]:
            folder = ROOT / 'examples/cann' / name
            original = runpy.run_path(str(folder / wrapper))
            packed = ast.parse(pack(folder / 'native'))
            fields = {node.targets[0].id: ast.literal_eval(node.value) for node in packed.body if isinstance(node, ast.Assign)}
            self.assertEqual(len(fields), 6)
            for key, text in fields.items():
                self.assertEqual(text, original[key], (name, key))

    def test_author_goal_is_preserved_and_modes_seed_differently(self):
        for name in ('vector-add', 'cube-matmul'):
            folder = ROOT / 'examples/cann' / name
            author = json.loads((folder / 'task.json').read_text())
            generated, reference, request, manifest = load_bundle(folder / 'task.json')
            optimized, _, _, _ = load_bundle(folder / 'task.json', 'optimize')
            self.assertTrue(generated['goal'].startswith(author['goal']))
            self.assertTrue(optimized['goal'].startswith(author['goal']))
            self.assertNotEqual(generated['seed_files']['solution.py'], optimized['seed_files']['solution.py'])
            self.assertEqual(optimized['seed_files']['solution.py'], (folder / 'baseline.py').read_text())
            self.assertEqual(generated, json.loads((folder / 'task.compiled.json').read_text()))
            self.assertEqual(reference, generated['seed_files']['reference/reference.py'])
            self.assertEqual(request['atol'], generated['contract']['correctness']['atol'])
            self.assertEqual(manifest['mode'], 'generate')

    def test_input_paths_and_invalid_cases_fail_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'example'
            shutil.copytree(ROOT / 'examples/cann/vector-add', folder)
            path = folder / 'task.json'
            original = json.loads(path.read_text())
            path.write_text(json.dumps({**original, 'case_file': '../case.json'}))
            with self.assertRaises(ValueError):
                load_bundle(path)
            path.write_text(json.dumps(original))
            case_path = folder / 'case.json'; case = json.loads(case_path.read_text())
            for field, value in [('atol', True), ('dtype', 'float16'), ('distribution', 'unknown'), ('shape', [0]), ('device', -1), ('seeds', [1]*5)]:
                case_path.write_text(json.dumps({**case, field: value}))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    load_bundle(path)
            case_path.unlink(); case_path.symlink_to(ROOT / 'examples/cann/vector-add/case.json')
            with self.assertRaises(ValueError):
                load_bundle(path)

    def test_native_packer_rejects_external_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'native'; shutil.copytree(ROOT / 'examples/cann/vector-add/native', folder)
            manifest = json.loads((folder / 'sources.json').read_text())
            manifest['fields']['kernel_src']['path'] = '../outside.cpp'
            (folder / 'sources.json').write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                pack(folder)
