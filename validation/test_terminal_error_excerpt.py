"""Source-bound error neighborhoods through real structured output; offline only.

Optional KERNELBRIDGE_EXCERPT_ATTEMPT and KERNELBRIDGE_EXCERPT_RUN point to
original read-only attempt/run directories. No model, native job or remote read
is permitted; source files and historical receipts are never rewritten.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'validation'))
import test_terminal_job_feedback as original

adapter = original.adapter


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def window_bytes(excerpt):
    return excerpt['text'].encode('utf-8') if excerpt['encoding'] == 'utf-8' else base64.b64decode(excerpt['data'], validate=True)


class TerminalErrorExcerptTests(unittest.TestCase):
    def setUp(self):
        self.h = original.TerminalJobFeedbackTests(methodName='runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def project(self, raw):
        self.h.result.update(message='Original wrapper failed; inspect retained diagnostics.', traceback='RuntimeError: original wrapper failure\n')
        self.h.seal('failed'); self.h.retain_run_log(raw)
        before = {path: digest(path.read_bytes()) for path in self.h.folder.iterdir() if path.is_file()}
        pipeline = self.h.pipeline()
        reply = self.h.call(pipeline, 'pipeline_run', stage='E', driver='original-launch')
        # Test the model-facing summary rather than calling the excerpt helper.
        feedback = reply['summary']['evaluation']
        self.assertEqual(feedback['native_candidate_sha256'], self.h.result['source_sha256'])
        self.assertTrue(all(digest(path.read_bytes()) == sha for path, sha in before.items()))
        return pipeline, feedback

    def assert_bound(self, pipeline, feedback, first, raw):
        excerpt = first['excerpt']; page = window_bytes(excerpt)
        self.assertGreater(len(page), 0)
        self.assertLessEqual(len(page), 3072)
        self.assertEqual(len(page), excerpt['length'])
        self.assertEqual(page, raw[excerpt['offset']:excerpt['offset'] + excerpt['length']])
        self.assertEqual(excerpt['sha256'], digest(page))
        self.assertEqual((excerpt['artifact_bytes'], excerpt['artifact_sha256']), (len(raw), digest(raw)))
        self.assertLessEqual(excerpt['line_end'] - excerpt['line_start'] + 1, 18)
        self.assertEqual(excerpt['line_scope'], 'artifact')
        self.assertEqual(first['read'], feedback['artifacts'][first['artifact']]['read'])
        self.assertEqual(excerpt['read']['arguments']['offset'], excerpt['offset'])
        self.assertEqual(excerpt['read']['arguments']['limit'], len(page))
        read = original.main(pipeline.config, {'id': uuid.uuid4().hex, **excerpt['read']})
        self.assertEqual(read['status'], 'completed', read)
        value = read['data']
        actual = value['text'].encode() if value['encoding'] == 'utf-8' else base64.b64decode(value['text'], validate=True)
        self.assertEqual(actual, page)
        self.assertEqual(value['sha256'], excerpt['artifact_sha256'])
        self.assertEqual(value['size_bytes'], excerpt['artifact_bytes'])
        artifact = feedback['artifacts'][first['artifact']]
        self.assertEqual(artifact['sha256'], excerpt['artifact_sha256'])
        self.assertEqual(artifact['read']['arguments']['path'], first['read']['arguments']['path'])
        return page

    def test_compiler_source_caret_and_notes_arrive_in_summary_without_unrelated_log_or_workspace_text(self):
        prefix = b'UNRELATED_SECRET_BEFORE\n' + (b'original compiler progress ' + b'.' * 120 + b'\n') * 72
        context = b'/original/frozen/source.cpp:55:40:\nerror: missing original symbol\n  55 | call(original_symbol);\n     |      ^~~~~~~~~~~~~~~\nnote: original overload is declared here\n'
        raw = prefix + context + b'build progress\n' * 80 + b'UNRELATED_SECRET_AFTER\n'
        # Unrelated mutable project source is deliberately different from logs.
        (self.h.root / 'source.cpp').write_text('MUTABLE_WORKSPACE_TEXT_MUST_NOT_APPEAR\n')
        pipeline, feedback = self.project(raw)
        first = feedback['first_compiler_diagnostic']
        self.assertEqual(first['text'], 'error: missing original symbol')
        page = self.assert_bound(pipeline, feedback, first, raw)
        self.assertGreater(first['excerpt']['read']['arguments']['offset'], 8192)
        self.assertIn(context, page)
        self.assertTrue(first['excerpt']['truncated_before'])
        self.assertTrue(first['excerpt']['truncated_after'])
        projected = json.dumps(feedback)
        for unrelated in ('UNRELATED_SECRET_BEFORE', 'UNRELATED_SECRET_AFTER', 'MUTABLE_WORKSPACE_TEXT_MUST_NOT_APPEAR'):
            self.assertNotIn(unrelated, projected)

    def test_multibyte_crlf_positions_reference_original_bytes_and_line_numbers(self):
        prefix = ('编译进度🙂\r\n' * 25).encode()
        raw = prefix + 'original.cpp:8:2:\r\nerror: 中文符号错误\r\n  调用(原符号);\r\n       ^~~~~\r\nnote: 后续说明\r\n'.encode() + b'done\r\n' * 25
        pipeline, feedback = self.project(raw)
        first = feedback['first_compiler_diagnostic']
        page = self.assert_bound(pipeline, feedback, first, raw)
        self.assertEqual(first['line'], 27)
        self.assertEqual((first['excerpt']['line_start'], first['excerpt']['line_end']), (24, 41))
        self.assertEqual(first['excerpt']['encoding'], 'utf-8')
        self.assertIn('调用(原符号)'.encode(), page)
        self.assertIn(b'\r\n', page)

    def test_long_utf8_line_keeps_error_and_aligns_both_clipped_edges(self):
        raw = ('前' * 4000 + ' error: 原始编译错误\n  source();\n  ^~~~~~~~\nnote: 后续' + '界' * 4000 + '\n').encode()
        pipeline, feedback = self.project(raw)
        first = feedback['first_compiler_diagnostic']
        page = self.assert_bound(pipeline, feedback, first, raw)
        self.assertEqual(first['excerpt']['encoding'], 'utf-8')
        self.assertIn('error: 原始编译错误'.encode(), page)
        self.assertIn(b'^~~~~~~~', page)
        self.assertTrue(first['excerpt']['first_line_partial'])
        self.assertTrue(first['excerpt']['last_line_partial'])
        self.assertTrue(first['truncated'])

    def test_non_utf8_log_is_not_lossily_decoded_and_retains_original_link(self):
        raw = b'prefix\xff\x80\nerror: exact binary-adjacent diagnostic\n  original();\n  ^~~~~~~~~~\nnote: exact\n'
        pipeline, feedback = self.project(raw)
        first = feedback['first_compiler_diagnostic']
        self.assertEqual(first['excerpt']['encoding'], 'base64')
        self.assertEqual(self.assert_bound(pipeline, feedback, first, raw), raw)
        self.assertNotIn('text', first['excerpt'])

    def test_sdk_and_runtime_diagnostics_use_the_same_source_bound_neighborhood(self):
        raw = b'original runtime context\n[ERROR] original SDK operation failed\n  original launch detail\n  ^ retained marker\nnote: runtime explanation\n'
        pipeline, feedback = self.project(raw)
        self.assertIsNone(feedback['first_compiler_diagnostic'])
        for field in ('first_runtime_error', 'first_sdk_diagnostic'):
            page = self.assert_bound(pipeline, feedback, feedback[field], raw)
            self.assertIn(b'original launch detail', page)
            self.assertIn(b'note: runtime explanation', page)
        self.assertEqual(feedback['error_excerpt'], feedback['first_runtime_error']['text'])

    def test_original_min_attempt_one_compiler_artifact_read_only(self):
        attempt_value = os.environ.get('KERNELBRIDGE_EXCERPT_ATTEMPT')
        run_value = os.environ.get('KERNELBRIDGE_EXCERPT_RUN')
        if not attempt_value or not run_value:
            self.skipTest('Supply original attempt and retained pipeline run for read-only replay')
        attempt, run = Path(attempt_value).resolve(), Path(run_value).resolve()
        watched = [*attempt.glob('*'), run / 'manifest.json', run / 'driver/request.json', *run.glob('driver/evaluation/*')]
        originals = {path: digest(path.read_bytes()) for path in watched if path.is_file()}
        result = json.loads((attempt / 'device-result.json').read_bytes())
        observation = json.loads((attempt / 'observation.json').read_bytes())
        summary = json.loads((attempt / 'result.json').read_bytes())
        self.assertEqual(digest((json.dumps(observation['result'], indent=2, allow_nan=False) + '\n').encode()), summary['remote_result_sha256'])
        self.assertEqual(result['source_sha256'], adapter.sha(attempt / 'candidate.py'))
        manifest = json.loads((run / 'manifest.json').read_bytes())
        local = self.h.root / 'original-attempt'; local.mkdir()
        for name in ('observation.json', 'run-log.json', 'run.log'):
            shutil.copyfile(attempt / name, local / name)
        request = json.loads((run / 'driver/request.json').read_bytes())
        self.assertEqual(hashlib.sha256(request['action_id'].encode()).hexdigest(), run.name)
        request['output'] = str(self.h.root / 'original-projection')
        expected_artifacts = {}
        for index, (relative, expected) in enumerate(sorted(result['build_diagnostics']['artifacts'].items())):
            name = f'build-{index:03d}' + Path(relative).suffix
            retained = run / 'driver/evaluation' / name
            self.assertEqual(manifest['driver/evaluation/' + name], expected)
            self.assertEqual(adapter.sha(retained), expected)
            expected_artifacts[relative] = retained
        def original_read(_transport, evidence, relative, expected, destination, remaining):
            raw = expected_artifacts[relative].read_bytes()
            self.assertEqual(digest(raw), expected)
            self.assertLessEqual(len(raw), remaining)
            destination.write_bytes(raw)
            return {'status': 'completed', 'bytes': len(raw), 'sha256': expected}
        evidence = {'native_candidate_sha256': result['source_sha256'], 'remote_result_sha256': summary['remote_result_sha256'],
            'original_handle': summary['original_handle'], 'original_job': summary['remote_attempt'].rstrip('/') + '/job'}
        with patch.object(adapter, 'transport', return_value=None), patch.object(adapter, 'download_artifact', side_effect=original_read):
            feedback = adapter.terminal_feedback(local, request, result, evidence, config={})
        first = feedback['first_compiler_diagnostic']; excerpt = first['excerpt']
        original_artifact = run / first['read']['arguments']['path']
        raw = original_artifact.read_bytes(); page = window_bytes(excerpt)
        self.assertEqual(manifest[first['read']['arguments']['path']], excerpt['artifact_sha256'])
        self.assertEqual(first['read']['arguments']['run'], run.name)
        self.assertEqual(page, raw[excerpt['offset']:excerpt['offset'] + excerpt['length']])
        required = b''.join(raw.splitlines(keepends=True)[73:78])
        self.assertTrue(required)
        self.assertEqual(len(required), 432)
        self.assertIn(required, page)
        self.assertIn(b'^~~~~~~~~~~~~~~~~~~~~~', page)
        self.assertTrue(all(digest(path.read_bytes()) == expected for path, expected in originals.items()))
        print(json.dumps({'offline_original_attempt': str(attempt), 'source_sha256': result['source_sha256'],
            'artifact_sha256': excerpt['artifact_sha256'], 'artifact_bytes': len(raw),
            'excerpt': excerpt, 'verified_original_read': excerpt['read'], 'new_device_jobs': 0}), flush=True)


if __name__ == '__main__':
    unittest.main()
