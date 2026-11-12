"""Pure source parsing and pre-dispatch admission; never execute candidate code."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/scatter'))
sys.path.insert(0, str(ROOT / 'examples/operator_pipeline'))
import preflight
import npu_benchmark
import npu_matmul_benchmark
import npu_trial

FIELDS = preflight.NATIVE_SOURCE_FIELDS
VALUES = {name: f'fixture source for {name}; 中文\n' for name in FIELDS}


def literal_module(values=None):
    return ''.join(f'{name} = {value!r}\n' for name, value in (values or VALUES).items())


class LiteralSourceFormatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kb-source-format-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.candidate = self.root / 'candidate.py'
        self.marker = self.root / 'candidate-must-not-execute'

    def parse_every_entry(self, raw):
        raw = raw.encode() if isinstance(raw, str) else raw
        self.candidate.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        parsed = [preflight.parse_literal_sources(raw, FIELDS)]
        parsed.extend(driver.sources(self.candidate) for driver in (npu_benchmark, npu_matmul_benchmark))
        self.assertEqual(self.candidate.read_bytes(), raw)
        self.assertEqual(hashlib.sha256(self.candidate.read_bytes()).hexdigest(), digest)
        self.assertFalse(self.marker.exists())
        return parsed

    def rejected_inputs(self):
        source = literal_module()
        first = FIELDS[0]
        rest = literal_module({key: value for key, value in VALUES.items() if key != first})
        danger = f"__import__('pathlib').Path({str(self.marker)!r}).write_text('executed')"
        return {
            'late_string': (source + "'not a module docstring'\n", 'statement'),
            'second_docstring': ("'first docstring'\n'second expression'\n" + source, 'statement'),
            'bytes_docstring': ("b'documentation'\n" + source, 'statement'),
            'fstring_docstring': ("f'documentation'\n" + source, 'statement'),
            'import': ('import pathlib\n' + source, 'statement'),
            'function': ('def extra():\n    pass\n' + source, 'statement'),
            'call': (danger + '\n' + source, 'statement'),
            'annotated': (f'{first}: str = "literal"\n' + rest, 'statement'),
            'multi_target': (f'{first} = other = "literal"\n' + rest, 'statement'),
            'tuple_target': (f'({first},) = ("literal",)\n' + rest, 'statement'),
            'duplicate': (source + f'{first} = "duplicate"\n', 'field'),
            'unknown': (source + 'extra_source = "unknown"\n', 'field'),
            'missing': (rest, 'missing'),
            'number': (f'{first} = 123\n' + rest, 'string'),
            'bytes_value': (f'{first} = b"literal"\n' + rest, 'string'),
            'list_value': (f'{first} = ["literal"]\n' + rest, 'string'),
            'call_value': (f'{first} = {danger}\n' + rest, 'literal'),
            'name_value': (f'{first} = unknown_name\n' + rest, 'literal'),
            'fstring_value': (f'{first} = f"literal"\n' + rest, 'literal'),
            'runtime_concat': (f'{first} = "left" + "right"\n' + rest, 'literal'),
            'syntax': (f'{first} = "unterminated\n' + rest, 'syntax'),
            'invalid_utf8': (b'# malformed encoding\n\xff\n' + source.encode(), 'encoding'),
        }

    def test_six_literals_comments_and_one_plain_leading_docstring_agree(self):
        danger = f"__import__('pathlib').Path({str(self.marker)!r}).write_text('executed')"
        for prefix in ('', '# comment before assignments\n\n',
                       '"""Module documentation.\n' + danger + '\n中文说明。\n"""\n',
                       '# leading comment\n\n"module documentation"\n# another comment\n'):
            with self.subTest(prefix=prefix):
                raw = prefix + literal_module()
                for parsed in self.parse_every_entry(raw):
                    self.assertEqual(parsed, VALUES)
                self.assertEqual(preflight.parse_literal_sources(raw, FIELDS), VALUES)

    def test_rejected_statements_fields_values_and_encoding_agree_without_execution(self):
        for name, (source, code) in self.rejected_inputs().items():
            with self.subTest(case=name):
                raw = source.encode() if isinstance(source, str) else source
                self.candidate.write_bytes(raw)
                errors = []
                readers = (lambda: preflight.parse_literal_sources(raw, FIELDS),
                           lambda: npu_benchmark.sources(self.candidate),
                           lambda: npu_matmul_benchmark.sources(self.candidate))
                for reader in readers:
                    with self.assertRaises(preflight.SourceFormatError) as raised:
                        reader()
                    errors.append(raised.exception.detail)
                    self.assertEqual(raised.exception.detail['code'], code)
                    self.assertTrue(str(raised.exception))
                    if code not in ('encoding', 'missing'):
                        self.assertGreaterEqual(raised.exception.detail['line'], 1)
                        self.assertGreaterEqual(raised.exception.detail['column'], 1)
                self.assertEqual(errors, [errors[0]] * 3)
                self.assertEqual(self.candidate.read_bytes(), raw)
                self.assertFalse(self.marker.exists())

    def test_docstring_preserves_implementation_digest_and_original_file_bytes(self):
        plain = literal_module().encode()
        documented = b'"""Documentation is not a new native implementation."""\n' + plain
        before = npu_trial.implementation_digest(self.parse_every_entry(plain)[0])
        after = npu_trial.implementation_digest(self.parse_every_entry(documented)[0])
        self.assertEqual(before, after)
        self.assertEqual(self.candidate.read_bytes(), documented)
        self.assertNotEqual(hashlib.sha256(plain).digest(), hashlib.sha256(documented).digest())

    def test_every_invalid_new_candidate_stops_before_transport_or_attempt_creation(self):
        state = self.root / 'attempts'
        config = {'candidate': str(self.candidate), 'state': str(state), 'mode': 'max',
                  'remote_root': '/never-used', 'attempt_namespace': 'format-unit',
                  'transport_module': str(self.root / 'never-import.py')}
        for name, (source, _) in self.rejected_inputs().items():
            with self.subTest(case=name):
                self.candidate.write_bytes(source.encode() if isinstance(source, str) else source)
                with patch.object(npu_trial, 'transport', side_effect=AssertionError('transport imported')) as transport, \
                     patch.object(npu_trial, 'invoke_remote', side_effect=AssertionError('remote invoked')) as invoke, \
                     self.assertRaises(preflight.SourceFormatError):
                    npu_trial.trial(config, {'id': name, 'arguments': {'hypothesis': 'fixture'}})
                transport.assert_not_called()
                invoke.assert_not_called()
                self.assertEqual({path.name for path in state.iterdir()}, {'lock'})
                self.assertFalse(self.marker.exists())

    def test_existing_attempt_recovery_precedes_new_source_admission(self):
        self.candidate.write_text('invalid current source, unrelated to original receipt')
        state = self.root / 'attempts'
        folder = state / 'attempt-0001'
        folder.mkdir(parents=True)
        intent = {'attempt': 1, 'action_id': 'original'}
        (folder / 'intent.json').write_text(json.dumps(intent))
        config = {'state': str(state), 'candidate': str(self.candidate)}
        with patch.object(npu_trial, 'sources', side_effect=AssertionError('new candidate parsed')), \
             patch.object(npu_trial, 'transport', return_value=Mock()) as transport, \
             patch.object(npu_trial, 'collect', return_value={'status': 'completed', 'data': 'original'}) as collect:
            self.assertEqual(npu_trial.trial(config, recover=1)['data'], 'original')
            self.assertEqual(collect.call_args.args[1], folder)
            transport.reset_mock(); collect.reset_mock()
            result = npu_trial.trial(config, {'id': 'new'})
            self.assertEqual(result['status'], 'unknown')
            transport.assert_not_called(); collect.assert_not_called()
            (folder / 'result.json').write_text(json.dumps({'status': 'completed', 'receipt': 'original'}))
            result = npu_trial.trial(config, {'id': 'original'})
            self.assertEqual(result['data']['receipt'], 'original')
            transport.assert_not_called(); collect.assert_not_called()
        self.assertFalse((state / 'attempt-0002').exists())

    def test_copied_standalone_tools_import_both_benchmarks_without_repository_dependencies(self):
        tools = self.root / 'standalone/tools'
        tools.mkdir(parents=True)
        for source in (ROOT / 'examples/scatter').glob('npu_*.py'):
            shutil.copyfile(source, tools / source.name)
        for name in ('diagnostic_policy.py', 'preflight.py'):
            shutil.copyfile(ROOT / 'examples/operator_pipeline' / name, tools / name)
        raw = '"""A standalone module docstring."""\n' + literal_module()
        self.candidate.write_text(raw)
        script = '''import json,sys
from pathlib import Path
folder=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(folder))
import preflight,npu_benchmark,npu_matmul_benchmark
assert Path(preflight.__file__).resolve().parent==folder
source=Path(sys.argv[2])
print(json.dumps([npu_benchmark.sources(source),npu_matmul_benchmark.sources(source)]))
'''
        completed = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(tools), str(self.candidate)],
                                   cwd=self.root, check=False, capture_output=True, text=True,
                                   env={**os.environ, 'PYTHONPATH': '', 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), [VALUES, VALUES])
        self.assertEqual(self.candidate.read_text(), raw)


if __name__ == '__main__':
    unittest.main()
