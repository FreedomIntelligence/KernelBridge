"""IR file-boundary regressions; simulated compiler, never CANN or a device.

KERNELBRIDGE_IR_REPLAY_RECEIPT optionally names the original read-only failed
receipt. Its SHA is pinned below; the test only summarizes its recorded failure.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_npu_diagnostics as fixture

diagnostics = fixture.diagnostics
EMPTY_SHA = hashlib.sha256(b'').hexdigest()
ORIGINAL_RECEIPT_SHA = '5a7201cd83bb43e2ab4c12ce32b61c77f0a9e86c4ad56dabbc0ee63a798d7948'


class IRInputValidationTests(unittest.TestCase):
    def setUp(self):
        self.h = fixture.NpuDiagnosticsTests(methodName='runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def assert_boundary(self, detail, *, role, code, minimum=0):
        self.assertEqual(detail['schema_version'], 'kernelbridge.ir-file-validation/v1')
        self.assertEqual(detail['role'], role)
        self.assertEqual(detail['code'], code)
        self.assertEqual(detail['expected'], {'regular': True, 'no_links': True, 'within_root': True,
                                              'min_bytes': minimum, 'max_bytes': diagnostics.MAX_ARTIFACT})

    def test_empty_forced_include_survives_original_recipe_collection_and_identity_recheck(self):
        h = self.h
        h.header.write_bytes(b'')
        separated = list(h.argv)
        for joined in (False, True):
            with self.subTest(joined=joined):
                h.argv = list(separated)
                if joined:
                    at = h.argv.index('-include')
                    h.argv[at:at + 2] = ['-include' + str(h.header)]
                original = h.write_recipe()
                originals = {p: p.read_bytes() for p in (h.recipe, h.source, h.header, h.job / 'candidate.py')}
                records = h.collect(); record = records[0]
                self.assertEqual(record['status'], 'completed')
                self.assertTrue(record['compiler_process_started'])
                self.assertEqual(record['original_argv'], original)
                self.assertEqual(record['steps'][0]['argv'], diagnostics.bitcode_command(
                    original, h.job / 'ir/kernel-000/emitted/kernel.bc'))
                self.assertEqual((record['compiler'], record['compiler_sha256'], record['language'], record['target']),
                                 (str(h.compiler), diagnostics._sha(h.compiler), 'cce', 'dav-c220-vec'))
                header = next(item for item in record['inputs'] if item['path'] == 'source/config.hpp')
                self.assertEqual(header, {'path': 'source/config.hpp', 'sha256': EMPTY_SHA, 'bytes': 0})
                self.assertIn({'role': 'forced_include', **header}, record['input_checks'])
                self.assertEqual(len(diagnostics.validated_ir_artifacts(h.job, records)), 2)
                self.assertIsNone(diagnostics.ir_failure_reason(records))
                self.assertEqual({p: p.read_bytes() for p in originals}, originals)
                shutil.rmtree(h.job / 'ir')

    def test_empty_source_is_valid_input_metadata_without_claiming_real_ir_capability(self):
        h = self.h; h.source.write_bytes(b'')
        original = h.write_recipe(); record = {}
        with patch.object(diagnostics.subprocess, 'Popen', side_effect=AssertionError('metadata must not compile')) as process:
            compiler, sources, inputs, output, language, target = diagnostics._recipe(original, h.job, h.sdk, record)
        process.assert_not_called()
        self.assertEqual(sources, [h.source]); self.assertIn(h.source, inputs)
        self.assertEqual((compiler, output, language, target), (h.compiler, h.job / 'build/kernel.o', 'cce', 'dav-c220-vec'))
        self.assertIn({'role': 'source', 'path': 'source/kernel.cpp', 'bytes': 0, 'sha256': EMPTY_SHA}, record['input_checks'])

    def test_success_exit_with_empty_bitcode_or_llvm_is_not_accepted(self):
        h = self.h
        for kind in ('bitcode', 'llvm'):
            with self.subTest(kind=kind):
                guard = "'-cc1' not in argv" if kind == 'bitcode' else "'-cc1' in argv"
                inserted = "if " + guard + ":\n    out.write_bytes(b''); print('simulated empty output'); sys.exit(0)\n"
                script = fixture.COMPILER.replace("if '-cc1' in argv:\n", inserted + "if '-cc1' in argv:\n", 1)
                h.compiler.write_text('#!' + sys.executable + '\n' + script)
                records = h.collect(); record = records[0]
                self.assertEqual(record['status'], 'unavailable'); self.assertEqual(record['accepted'], [])
                self.assertTrue(record['compiler_process_started'])
                self.assertTrue(all(step['returncode'] == 0 for step in record['steps']))
                detail = record['failure'] if kind == 'bitcode' else record['steps'][-1]['validation_failure']
                self.assert_boundary(detail, role='artifact', code='empty_file', minimum=1)
                self.assertEqual(detail['phase'], 'artifact_validation')
                self.assertEqual(detail['observed'], {'bytes': 0, 'sha256': EMPTY_SHA})
                self.assertTrue(detail['path'].endswith('.bc' if kind == 'bitcode' else '.ll'))
                self.assertEqual(diagnostics.validated_ir_artifacts(h.job, records), {})
                reason = diagnostics.ir_failure_reason(records)
                self.assertIn('IR file is empty', reason)
                self.assertIn('bytes=0', reason)
                for step in record['steps']:
                    self.assertIn(step['log'], reason)
                    self.assertEqual(diagnostics._sha(h.job / step['log']), step['log_sha256'])
                if kind == 'llvm': self.assertFalse((h.job / 'ir/kernel-000/emitted/kernel.ll').exists())
                shutil.rmtree(h.job / 'ir')

    def test_empty_retained_bitcode_is_rejected_before_any_compiler_step(self):
        h = self.h; retained = h.retained(); retained.write_bytes(b'')
        with patch.object(diagnostics.subprocess, 'Popen', side_effect=AssertionError('empty artifact must not compile')) as process:
            record = h.collect()[0]
        process.assert_not_called()
        self.assertEqual(record['steps'], []); self.assertFalse(record['compiler_process_started'])
        self.assertEqual(record['accepted'], []); self.assertEqual(record['status'], 'unavailable')
        self.assert_boundary(record['failure'], role='artifact', code='empty_file', minimum=1)
        self.assertEqual(record['failure']['path'], str(retained.relative_to(h.job)))
        self.assertEqual(record['failure']['observed'], {'bytes': 0, 'sha256': EMPTY_SHA})
        self.assertNotIn('compiler logs', diagnostics.ir_failure_reason([record]))

    def test_unsafe_or_uninspectable_inputs_are_rejected_without_reading_or_hashing(self):
        h = self.h
        outside = h.root / 'outside.hpp'; outside.write_text('outside data must not be read')
        linked = h.job / 'linked.hpp'; linked.symlink_to(outside)
        linked_inside = h.job / 'linked-inside.hpp'; linked_inside.symlink_to(h.header)
        loop = h.job / 'loop.hpp'; loop.symlink_to(loop)
        large = h.job / 'large.hpp'
        with large.open('wb') as stream: stream.truncate(diagnostics.MAX_ARTIFACT + 1)
        paths = [(outside, {'outside_root'}), (Path('relative.hpp'), {'outside_root'}),
                 (linked, {'linked_path'}), (linked_inside, {'linked_path'}),
                 (h.job / 'source/../source/config.hpp', {'linked_path'}),
                 (loop, {'linked_path', 'unreadable'}), (h.source.parent, {'not_regular'}),
                 (h.job / 'missing.hpp', {'unreadable'}), (large, {'size_limit'})]
        for path, codes in paths:
            with self.subTest(path=str(path)), patch.object(diagnostics, '_sha', side_effect=AssertionError('unsafe file must not be hashed')) as hashed:
                with self.assertRaises(diagnostics.IRFileValidationError) as raised:
                    diagnostics._identity(path, h.job, allow_empty=True, role='forced_include')
                detail = raised.exception.details
                self.assertIn(detail['code'], codes)
                self.assert_boundary(detail, role='forced_include', code=detail['code'])
                self.assertEqual(detail['phase'], 'input_validation')
                hashed.assert_not_called()
                if path == large:
                    self.assertEqual(detail['observed'], {'bytes': diagnostics.MAX_ARTIFACT + 1})
                else:
                    self.assertNotIn('observed', detail)
                self.assertNotIn('sha256', detail.get('observed', {}))
                self.assertIn(detail['path'], str(raised.exception))
                json.dumps(detail, allow_nan=False)

    def test_exact_size_bound_still_accepts_regular_file_for_both_roles(self):
        h = self.h; path = h.job / 'at-limit.bc'
        with path.open('wb') as stream: stream.truncate(diagnostics.MAX_ARTIFACT)
        with patch.object(diagnostics, '_sha', side_effect=AssertionError('metadata check does not hash')):
            self.assertEqual(diagnostics._regular(path, h.job), path)
            self.assertEqual(diagnostics._regular(path, h.job, role='source', allow_empty=True), path)

    def test_missing_forced_include_keeps_original_identity_and_precommand_failure(self):
        h = self.h; h.header.unlink()
        original = h.write_recipe()
        with patch.object(diagnostics.subprocess, 'Popen', side_effect=AssertionError('input rejection must not compile')) as process:
            record = h.collect()[0]
        process.assert_not_called()
        self.assertFalse(record['compiler_process_started']); self.assertEqual(record['steps'], [])
        self.assertEqual(record['status'], 'unavailable'); self.assertEqual(record['accepted'], [])
        self.assertEqual(record['original_argv'], original)
        self.assertEqual(record['compiler_sha256'], diagnostics._sha(h.compiler))
        self.assertEqual(record['recipe_sha256'], diagnostics._sha(h.recipe))
        self.assertEqual(record['inputs'], [diagnostics._identity(h.source, h.job)])
        self.assert_boundary(record['failure'], role='forced_include', code='unreadable')
        self.assertEqual(record['failure']['path'], 'source/config.hpp')
        self.assertEqual(record['failure']['phase'], 'input_validation')
        self.assertIn('os_error', record['failure'])
        summary = diagnostics.ir_failure_reason([record])
        self.assertIn('input_validation', summary); self.assertIn('forced_include source/config.hpp', summary)
        self.assertIn('ir/receipt.json', summary); self.assertNotIn('compiler logs', summary)

    def test_candidate_and_build_recipe_still_require_nonempty_bytes(self):
        h = self.h
        for path, role in ((h.job / 'candidate.py', 'candidate_wrapper'), (h.recipe, 'build_recipe')):
            with self.subTest(role=role):
                old = path.read_bytes(); path.write_bytes(b'')
                try:
                    with patch.dict(os.environ, {'ASCEND_HOME_PATH': str(h.sdk)}):
                        with self.assertRaises(diagnostics.IRFileValidationError) as raised:
                            diagnostics.collect_ir(h.job)
                    self.assert_boundary(raised.exception.details, role=role, code='empty_file', minimum=1)
                    self.assertEqual(raised.exception.details['observed'], {'bytes': 0, 'sha256': EMPTY_SHA})
                finally:
                    path.write_bytes(old)
                    if (h.job / 'ir').exists(): shutil.rmtree(h.job / 'ir')

    def test_changed_formerly_empty_input_invalidates_original_accepted_artifacts(self):
        h = self.h; h.header.write_bytes(b'')
        records = h.collect(); self.assertEqual(len(diagnostics.validated_ir_artifacts(h.job, records)), 2)
        h.header.write_text('#define CHANGED 1\n')
        with self.assertRaisesRegex(ValueError, 'IR source input changed'):
            diagnostics.validated_ir_artifacts(h.job, records)

    def test_empty_output_cannot_be_promoted_by_a_previous_completed_receipt(self):
        h = self.h; records = h.collect()
        for relative in diagnostics.validated_ir_artifacts(h.job, records):
            path = h.job / relative; raw = path.read_bytes()
            with self.subTest(path=relative):
                path.write_bytes(b'')
                try:
                    with self.assertRaisesRegex(ValueError, 'Accepted IR manifest failed revalidation: IR file is empty'):
                        diagnostics.validated_ir_artifacts(h.job, records)
                finally: path.write_bytes(raw)

    def test_failure_summary_uses_first_failed_recipe_and_only_recorded_log_artifacts(self):
        h = self.h; h.write_recipe('partial_compile')
        record = h.collect()[0]
        record['steps'].append({'log': 'never-created.log', 'status': 'started'})
        snapshot = copy.deepcopy(record)
        summary = diagnostics.ir_failure_reason([{'status': 'completed'}, record,
            {'status': 'unavailable', 'recipe': 'later.mk', 'reason': 'not the first failure', 'steps': []}])
        self.assertIn('returncode=2', summary)
        self.assertIn(record['steps'][0]['log'], summary)
        self.assertNotIn('never-created.log', summary); self.assertNotIn('later.mk', summary)
        self.assertEqual(record, snapshot)
        self.assertLessEqual(len(summary), 2048)

    def test_original_failure_receipt_is_read_only_and_does_not_invent_missing_file_identity(self):
        value = os.environ.get('KERNELBRIDGE_IR_REPLAY_RECEIPT')
        if not value: self.skipTest('Supply original IR receipt for read-only replay')
        path = Path(value).resolve(strict=True)
        before = path.read_bytes()
        self.assertEqual(hashlib.sha256(before).hexdigest(), ORIGINAL_RECEIPT_SHA)
        records = json.loads(before); snapshot = copy.deepcopy(records)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record['status'], 'unavailable'); self.assertEqual(record['steps'], [])
        self.assertEqual(record['accepted'], [])
        self.assertNotIn('failure', record); self.assertNotIn('input_checks', record)
        with patch.object(diagnostics, '_sha', side_effect=AssertionError('receipt summary must not access original inputs')), \
             patch.object(diagnostics.subprocess, 'Popen', side_effect=AssertionError('receipt replay must not compile')):
            summary = diagnostics.ir_failure_reason(records)
        self.assertEqual(records, snapshot)
        self.assertIn('IR input/output exceeds its size bound or is empty', summary)
        self.assertIn('recipe_validation', summary)
        self.assertNotIn('compiler logs', summary); self.assertNotIn('forced_include', summary)
        self.assertNotIn('bytes=', summary)
        self.assertEqual(path.read_bytes(), before)
        print(json.dumps({'read_only_receipt': str(path), 'sha256': ORIGINAL_RECEIPT_SHA,
                          'summary': summary, 'compiler_steps': 0, 'original_input_failure_identity': 'not recorded'}), flush=True)


if __name__ == '__main__':
    unittest.main()
