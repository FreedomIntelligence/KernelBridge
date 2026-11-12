"""IR provenance/error-path regression with an explicitly simulated compiler.

The fixture exercises real subprocess/receipt/file handling, not CANN capability.
Actual cce bitcode support still requires qualification with the pinned compiler.
"""
import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples/scatter'))
import npu_diagnostics as diagnostics


COMPILER = r'''
import json, sys
from pathlib import Path
argv = sys.argv[1:]
out = Path(argv[argv.index('-o') + 1])
if '-cc1' in argv:
    fixture = json.loads(Path(argv[-1]).read_bytes()[4:])
    behavior = fixture['behavior']
    source = '/wrong/source.cpp' if behavior == 'wrong_source' else fixture['source']
    triple = 'x86_64-unknown-linux-gnu' if behavior == 'host' else 'hiipu64-hisilicon-cce'
    cpu = 'dav-c220-cube' if behavior == 'wrong_cpu' else fixture['target']
    out.write_text('source_filename = ' + json.dumps(source) + '\n'
        + 'target triple = "' + triple + '"\n'
        + 'define void @fixture() #0 { ret void }\n'
        + 'attributes #0 = { "target-cpu"="' + cpu + '" }\n')
    if behavior == 'partial_decode':
        print('decoder failed after writing partial output'); sys.exit(3)
    if behavior == 'override': print('warning: overriding the module target triple')
else:
    assert '-c' in argv and '-S' not in argv, 'Original object-compilation mode must survive'
    assert '--cce-aicore-only' in argv and '-x' in argv and argv[argv.index('-x') + 1] == 'cce'
    behavior = next((v.split('=', 1)[1] for v in argv if v.startswith('-DFIXTURE_BEHAVIOR=')), 'success')
    fixture = {'behavior': behavior, 'source': next(v for v in argv if v.endswith('.cpp')),
               'target': next(v.split('=', 1)[1] for v in argv if v.startswith('--cce-aicore-arch='))}
    out.write_bytes(b'BC\xc0\xde' + json.dumps(fixture).encode())
    if behavior == 'partial_compile':
        print("error: unsupported option '-emit-llvm'"); sys.exit(2)
print('simulated compiler protocol fixture; not device qualification')
'''


class NpuDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.job = self.root / 'frozen-job'; self.job.mkdir()
        self.sdk = self.root / 'sdk'; self.sdk.mkdir()
        self.compiler = self.sdk / 'bisheng'
        self.compiler.write_text('#!' + sys.executable + '\n' + COMPILER)
        self.compiler.chmod(0o700)
        self.source = self.job / 'source/kernel.cpp'; self.source.parent.mkdir()
        self.source.write_text('// compile-only provenance fixture\n')
        self.header = self.job / 'source/config.hpp'; self.header.write_text('#define FIXTURE 1\n')
        (self.job / 'candidate.py').write_text('# frozen fixture candidate\n')
        (self.job / 'build').mkdir()
        self.recipe = self.job / 'build/kernel.mk'
        self.argv = [str(self.compiler), '-c', '-O3', '-x', 'cce', '-save-temps', '--save-temp-files',
                     '--cce-aicore-arch=dav-c220-vec', '--cce-aicore-only', '-DFIXTURE=1',
                     '-include', str(self.header), '-I', str(self.source.parent), '-MD', '-MF',
                     str(self.job / 'build/kernel.d'), str(self.source), '-o', str(self.job / 'build/kernel.o')]
        self.write_recipe()

    def write_recipe(self, behavior='success'):
        argv = [*self.argv, '-DFIXTURE_BEHAVIOR=' + behavior]
        self.recipe.write_text('kernel.o:\n\t' + shlex.join(argv) + '\n')
        return argv

    def collect(self):
        with patch.dict(os.environ, {'ASCEND_HOME_PATH': str(self.sdk)}):
            result = diagnostics.collect_ir(self.job)
        self.assertEqual(result, json.loads((self.job / 'ir/receipt.json').read_text()))
        return result

    def retained(self, behavior='success'):
        path = self.job / 'build/kernel-cce-hiipu64-hisilicon-cce-dav-c220-vec.bc'
        path.write_bytes(b'BC\xc0\xde' + json.dumps({'behavior': behavior, 'source': str(self.source),
            'target': 'dav-c220-vec'}).encode())
        return path

    def test_preserved_mode_emits_only_validated_source_and_target_bound_ir(self):
        original = self.write_recipe()
        records = self.collect(); record = records[0]
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['strategy'], 'preserved_mode_bitcode_capability_attempt')
        emitted = record['steps'][0]['argv']
        expected = diagnostics.bitcode_command(original, self.job / 'ir/kernel-000/emitted/kernel.bc')
        self.assertEqual(emitted, expected)
        self.assertIn('-c', emitted); self.assertNotIn('-S', emitted)
        self.assertIn('-DFIXTURE=1', emitted); self.assertIn(str(self.header), emitted)
        self.assertEqual(emitted[emitted.index('-x') + 1], 'cce')
        self.assertTrue(all(step['returncode'] == 0 for step in record['steps']))
        accepted = diagnostics.validated_ir_artifacts(self.job, records)
        self.assertEqual({Path(path).suffix for path in accepted}, {'.bc', '.ll'})
        self.assertEqual(record['accepted'][0]['llvm']['target_cpu'], 'dav-c220-vec')

    def test_original_retained_bitcode_is_preferred_without_compile_replay(self):
        retained = self.retained()
        records = self.collect(); record = records[0]
        self.assertEqual(record['strategy'], 'original_retained_bitcode')
        self.assertEqual([step['operation'] for step in record['steps']], ['decode_bitcode'])
        self.assertEqual(record['accepted'][0]['retained_from']['sha256'], diagnostics._sha(retained))
        self.assertEqual(len(diagnostics.validated_ir_artifacts(self.job, records)), 2)

    def test_invalid_retained_bitcode_falls_back_without_accepting_the_file(self):
        self.retained().write_bytes(b'not LLVM bitcode')
        records = self.collect()
        self.assertEqual(records[0]['status'], 'completed')
        self.assertEqual(records[0]['strategy'], 'preserved_mode_bitcode_capability_attempt')
        self.assertIn('not LLVM bitcode', records[0]['retained_errors'][0]['error'])
        self.assertTrue(all('/emitted/' in path for path in diagnostics.validated_ir_artifacts(self.job, records)))

    def test_failed_compiler_partial_bitcode_is_not_diagnostic_success(self):
        self.write_recipe('partial_compile'); records = self.collect()
        self.assertEqual(records[0]['status'], 'unavailable')
        self.assertEqual(records[0]['steps'][0]['returncode'], 2)
        self.assertIn("unsupported option '-emit-llvm'", (self.job / records[0]['steps'][0]['log']).read_text())
        self.assertEqual(diagnostics.validated_ir_artifacts(self.job, records), {})
        self.assertFalse((self.job / 'ir/kernel-000/emitted/kernel.bc').exists())

    def test_failed_decoder_partial_text_is_not_diagnostic_success(self):
        self.write_recipe('partial_decode'); records = self.collect()
        self.assertEqual(records[0]['status'], 'unavailable')
        self.assertEqual(records[0]['steps'][-1]['returncode'], 3)
        self.assertEqual(diagnostics.validated_ir_artifacts(self.job, records), {})
        self.assertFalse((self.job / 'ir/kernel-000/emitted/kernel.ll').exists())

    def test_success_exit_does_not_accept_wrong_source_target_or_target_override(self):
        for behavior in ('wrong_source', 'wrong_cpu', 'host', 'override'):
            with self.subTest(behavior=behavior):
                self.write_recipe(behavior)
                records = self.collect()
                self.assertEqual(records[0]['status'], 'unavailable')
                self.assertEqual(records[0]['steps'][-1]['returncode'], 0)
                self.assertIn('validation_error', records[0]['steps'][-1])
                self.assertEqual(diagnostics.validated_ir_artifacts(self.job, records), {})
                shutil.rmtree(self.job / 'ir')

    def test_artifact_input_recipe_candidate_compiler_tampering_is_rejected(self):
        retained = self.retained(); records = self.collect()
        paths = [retained, self.source, self.header, self.recipe, self.job / 'candidate.py', self.compiler,
                 *(self.job / path for path in diagnostics.validated_ir_artifacts(self.job, records))]
        for path in paths:
            with self.subTest(path=path):
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b' changed')
                    with self.assertRaisesRegex(ValueError, 'manifest failed revalidation'):
                        diagnostics.validated_ir_artifacts(self.job, records)
                finally: path.write_bytes(original)

    def test_failed_step_cannot_be_promoted_by_completed_status(self):
        records = self.collect(); changed = copy.deepcopy(records)
        changed[0]['steps'][0]['returncode'] = 2
        with self.assertRaisesRegex(ValueError, 'failed command'):
            diagnostics.validated_ir_artifacts(self.job, changed)

    def test_llvm_utf8_byte_escapes_preserve_original_source_identity(self):
        source = self.job / 'source/算子.cpp'; source.write_text('// original\n')
        escaped = ''.join(chr(value) if 32 <= value < 127 else '\\' + format(value, '02X')
                          for value in str(source).encode('utf-8'))
        ir = self.job / 'unicode.ll'
        ir.write_text('source_filename = "' + escaped + '"\n'
                      'target triple = "hiipu64-hisilicon-cce"\n'
                      'define void @fixture() #0 { ret void }\n'
                      'attributes #0 = { "target-cpu"="dav-c220-vec" }\n')
        self.assertEqual(diagnostics._llvm_metadata(ir, [source], 'dav-c220-vec')['source_filename'], str(source))

    def test_unsupported_language_or_outside_source_is_explicitly_unavailable(self):
        for kind in ('language', 'source'):
            with self.subTest(kind=kind):
                original = self.recipe.read_text()
                if kind == 'language': self.recipe.write_text(original.replace('-x cce', '-x c++'))
                else:
                    other = self.root / 'outside.cpp'; other.write_text('// unrelated\n')
                    self.recipe.write_text(original.replace(str(self.source), str(other)))
                records = self.collect()
                self.assertEqual(records[0]['status'], 'unavailable')
                self.assertEqual(records[0]['steps'], [])
                self.assertEqual(diagnostics.validated_ir_artifacts(self.job, records), {})
                shutil.rmtree(self.job / 'ir'); self.recipe.write_text(original)

    def test_retained_symlink_is_not_followed(self):
        outside = self.root / 'outside.bc'; outside.write_bytes(b'BC\xc0\xde unrelated')
        (self.job / 'build/kernel.bc').symlink_to(outside)
        records = self.collect()
        self.assertEqual(records[0]['status'], 'unavailable')
        self.assertIn('non-linked', records[0]['reason'])
        self.assertEqual(records[0]['steps'], [])

    def test_collection_cannot_overwrite_a_previous_receipt(self):
        self.collect()
        before = (self.job / 'ir/receipt.json').read_bytes()
        with patch.dict(os.environ, {'ASCEND_HOME_PATH': str(self.sdk)}):
            with self.assertRaises(FileExistsError): diagnostics.collect_ir(self.job)
        self.assertEqual((self.job / 'ir/receipt.json').read_bytes(), before)


if __name__ == '__main__': unittest.main()
