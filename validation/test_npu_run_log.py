"""Original-job log capture through real local read-only transport scripts."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples/scatter'))
import npu_trial
import npu_pipeline_driver as adapter
import test_npu_pipeline_driver as driver_fixture
import test_npu_diagnostic_projection as diagnostic_fixture


class NpuRunLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / 'local/attempt-0001'; self.folder.mkdir(parents=True)
        self.remote = self.root / 'remote/original'; (self.remote / 'job').mkdir(parents=True)
        data = b'# original fixture candidate, never executed\n'
        self.source_sha = hashlib.sha256(data).hexdigest()
        self.handle = {'pid': 2147483647, 'argv': ['original'], 'started_ns': 1794401504504640402,
                       'attempt': str(self.remote)}
        self.intent = {'attempt': 1, 'action_id': 'original-action', 'source_sha256': self.source_sha,
                       'remote_attempt': str(self.remote), 'hypothesis': 'original frozen hypothesis'}
        self.result = {'status': 'failed', 'correct': False, 'source_sha256': self.source_sha,
                       'message': 'Generic async failure', 'cases': []}
        for folder in (self.folder, self.remote):
            (folder / 'candidate.py').write_bytes(data)
            npu_trial.save(folder / 'intent.json', self.intent)
        npu_trial.save(self.remote / 'launch.json', self.handle)
        npu_trial.save(self.folder / 'launch.json', {'existing': False, 'handle': self.handle})
        npu_trial.save(self.remote / 'job/result.json', self.result)
        self.data = b'[ERROR] original device failure at entry\n' + ('eager noise 中文\n' * 24000).encode()
        (self.remote / 'run.log').write_bytes(self.data)
        self.config = {'state': str(self.folder.parent), 'candidate': str(self.folder / 'candidate.py'),
                       'mode': 'max', 'wait_seconds': 0, 'poll_seconds': 0}
        self.calls = []

    def transport(self, argv, stdin=b'', timeout=45):
        self.calls.append(argv[3])
        self.assertIn(argv[3], (npu_trial.COLLECT, npu_trial.READ_RUN_LOG), 'Must never dispatch a device job')
        result = subprocess.run([sys.executable, *argv[1:]], input=stdin, capture_output=True, timeout=timeout)
        return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}

    def observe(self):
        return npu_trial.invoke_remote(self.transport, npu_trial.COLLECT, str(self.remote))

    def capture(self, observation=None, transport=None):
        return npu_trial.capture_run_log(self.folder, self.intent, observation or self.observe(), transport or self.transport)

    def test_full_log_survives_long_json_tail_in_bounded_pages_with_exact_hash(self):
        result = npu_trial.collect(self.config, self.folder, self.transport)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['data']['status'], 'failed')
        log = result['data']['run_log']
        self.assertEqual(log['status'], 'completed'); self.assertTrue(log['complete'])
        self.assertEqual(log['bytes'], len(self.data))
        self.assertEqual(log['sha256'], hashlib.sha256(self.data).hexdigest())
        self.assertEqual((self.folder / 'run.log').read_bytes(), self.data)
        self.assertNotIn('original device failure', json.loads((self.folder / 'observation.json').read_text())['log_tail'])
        self.assertEqual(self.calls.count(npu_trial.READ_RUN_LOG), 2)
        self.assertEqual(log['original_handle']['started_ns'], '1794401504504640402')
        self.assertNotIn('identity', log['observed'])
        self.assertIn('identity', json.loads((self.folder / 'run-log.json').read_text())['observed'])

    def test_source_handle_intent_result_and_log_changes_are_rejected_without_launch(self):
        originals = {p: p.read_bytes() for p in self.remote.rglob('*') if p.is_file()}
        mutations = {
            'source': lambda: (self.remote / 'candidate.py').write_bytes(b'other candidate'),
            'handle': lambda: npu_trial.save(self.remote / 'launch.json', dict(self.handle, pid=1)),
            'intent': lambda: npu_trial.save(self.remote / 'intent.json', dict(self.intent, action_id='other')),
            'result': lambda: npu_trial.save(self.remote / 'job/result.json', dict(self.result, message='changed')),
            'log': lambda: (self.remote / 'run.log').write_bytes(self.data + b'new bytes'),
        }
        for kind, mutate in mutations.items():
            with self.subTest(kind=kind):
                for path, data in originals.items(): path.write_bytes(data)
                observed = self.observe(); mutate()
                record = self.capture(observed)
                self.assertEqual(record['status'], 'unavailable', record)
                self.assertFalse((self.folder / 'run.log').exists())
                self.assertFalse((self.folder / 'run.log.tmp').exists())
                self.assertIn('never launch a replacement', record['recovery'])

    def test_symlink_log_is_not_followed(self):
        observed = self.observe()
        outside = self.root / 'outside.log'; outside.write_bytes(self.data)
        (self.remote / 'run.log').unlink(); (self.remote / 'run.log').symlink_to(outside)
        record = self.capture(observed)
        self.assertEqual(record['status'], 'unavailable')
        self.assertIn('linked', record['reason'])
        self.assertFalse((self.folder / 'run.log').exists())

    def test_page_length_and_final_hash_are_verified(self):
        observed = self.observe()
        for mode in ('length', 'hash'):
            def corrupt(argv, **kwargs):
                reply = self.transport(argv, **kwargs)
                if argv[3] == npu_trial.READ_RUN_LOG:
                    page = json.loads(reply['stdout']); data = base64.b64decode(page['data'])
                    data = data[:-1] if mode == 'length' else b'X' + data[1:]
                    page.update(data=base64.b64encode(data).decode(), bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                    reply['stdout'] = json.dumps(page).encode()
                return reply
            with self.subTest(mode=mode):
                record = self.capture(observed, corrupt)
                self.assertEqual(record['status'], 'unavailable')
                self.assertFalse((self.folder / 'run.log').exists())
                self.assertFalse((self.folder / 'run.log.tmp').exists())

    def test_explicit_bound_preserves_head_and_tail_without_claiming_complete(self):
        observed = self.observe()
        before = len(self.calls)
        with patch.object(npu_trial, 'MAX_LOG_BYTES', len(self.data) - 1):
            record = self.capture(observed)
        self.assertEqual(record['status'], 'partial'); self.assertFalse(record['complete'])
        self.assertIn('local retention bound', record['reason'])
        self.assertEqual(len(self.calls), before + 2)
        self.assertEqual((self.folder / 'run.log.head').read_bytes(), self.data[:npu_trial.LOG_PAGE_BYTES])
        self.assertEqual((self.folder / 'run.log.tail').read_bytes(), self.data[-npu_trial.LOG_PAGE_BYTES:])
        self.assertEqual(record['observed']['sha256'], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(record['fragments']['run.log.tail']['offset'], len(self.data) - npu_trial.LOG_PAGE_BYTES)
        self.assertFalse((self.folder / 'run.log').exists())
        npu_trial.save(self.folder / 'observation.json', observed)
        evidence = {'native_candidate_sha256': self.source_sha, 'remote_result_sha256': observed['result_sha256'],
                    'original_handle': self.handle, 'original_job': str(self.remote / 'job')}
        feedback = adapter.terminal_feedback(self.folder, {'output': str(self.root / 'feedback'), 'action_id': 'read-fragments'}, self.result, evidence)
        self.assertEqual(feedback['run_log']['status'], 'partial')
        self.assertEqual(feedback['run_log']['original_bytes'], len(self.data))
        self.assertEqual(feedback['first_runtime_error']['artifact'], 'run.log.head')
        self.assertIn('original device failure', feedback['first_runtime_error']['text'])
        self.assertIn('read', feedback['run_log']['fragments']['run.log.head']['artifact'])

    def test_active_process_snapshot_is_explicitly_partial_and_empty_final_log_is_valid(self):
        observed = self.observe(); observed['log']['complete'] = False
        partial = self.capture(observed)
        self.assertEqual(partial['status'], 'partial'); self.assertFalse(partial['complete'])
        (self.remote / 'run.log').write_bytes(b'')
        complete = self.capture()
        self.assertEqual(complete['status'], 'completed'); self.assertTrue(complete['complete'])
        self.assertEqual((self.folder / 'run.log').read_bytes(), b'')

    def test_log_transfer_failure_does_not_erase_terminal_receipt_or_redispatch(self):
        def interrupted(argv, **kwargs):
            if argv[3] == npu_trial.READ_RUN_LOG: raise TimeoutError('Read transport interrupted')
            return self.transport(argv, **kwargs)
        first = npu_trial.collect(self.config, self.folder, interrupted)
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(first['data']['status'], 'failed')
        self.assertEqual(first['data']['run_log']['status'], 'unavailable')
        with patch.object(npu_trial, 'transport', return_value=self.transport):
            repeated = npu_trial.trial(self.config, {'id': 'original-action'})
            recovered = npu_trial.trial(self.config, recover=1)
        self.assertEqual(repeated, first)
        self.assertEqual(recovered['data']['run_log']['status'], 'completed')
        self.assertEqual(recovered['data']['original_handle'], first['data']['original_handle'])
        self.assertNotIn(npu_trial.LAUNCH, self.calls)

    def test_unobserved_job_remains_unknown_and_blocks_new_attempt(self):
        def unavailable(*args, **kwargs): raise TimeoutError('Original job unknown')
        with patch.object(npu_trial, 'transport', return_value=unavailable):
            outcome = npu_trial.trial(self.config, recover=1)
            next_attempt = npu_trial.trial(self.config, {'id': 'new-action'})
        self.assertEqual(outcome['status'], 'unknown'); self.assertEqual(next_attempt['status'], 'unknown')
        self.assertFalse((self.folder / 'result.json').exists())
        self.assertFalse((self.folder.parent / 'attempt-0002').exists())


class NpuLogRecoveryProjectionTests(unittest.TestCase):
    def setUp(self):
        driver_fixture.NpuPipelineDriverTest.setUp(self)
        self.remote = self.root / 'remote'; (self.remote / 'job').mkdir(parents=True)
        self.handle = dict(self.handle, pid=2147483647, attempt=str(self.remote))
        self.request['handle'] = adapter.wire_handle(self.handle)
        intent = json.loads((self.folder / 'intent.json').read_text())
        self.intent = dict(intent, remote_attempt=str(self.remote), hypothesis='original')
        for folder in (self.folder, self.remote): npu_trial.save(folder / 'intent.json', self.intent)
        (self.remote / 'candidate.py').write_bytes((self.folder / 'candidate.py').read_bytes())
        npu_trial.save(self.remote / 'job/result.json', self.result)
        npu_trial.save(self.remote / 'launch.json', self.handle)
        npu_trial.save(self.folder / 'launch.json', {'handle': self.handle})
        self.initial = b'[ERROR] retained original log prefix\n'
        (self.remote / 'run.log').write_bytes(self.initial)
        self.calls = []
        observed = npu_trial.invoke_remote(self.transport, npu_trial.COLLECT, str(self.remote))
        observed['log']['complete'] = False
        npu_trial.save(self.folder / 'observation.json', observed)
        npu_trial.capture_run_log(self.folder, self.intent, observed, self.transport)
        summary = json.loads((self.folder / 'result.json').read_text())
        npu_trial.save(self.folder / 'result.json', dict(summary, original_handle=self.handle, remote_attempt=str(self.remote)))

    def transport(self, argv, stdin=b'', timeout=45):
        self.calls.append(argv[3])
        self.assertIn(argv[3], (npu_trial.COLLECT, npu_trial.READ_RUN_LOG))
        result = subprocess.run([sys.executable, *argv[1:]], input=stdin, capture_output=True, timeout=timeout)
        return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}

    def test_explicit_collect_finishes_log_without_changing_original_F_or_relaunching(self):
        sealed = {name: (self.folder / name).read_bytes() for name in ('result.json', 'device-result.json', 'candidate.py', 'intent.json')}
        before = adapter.execute(self.config, 'check', self.request)
        full = self.initial + b'original job final output\n' * 10000
        (self.remote / 'run.log').write_bytes(full)
        with patch.object(adapter, 'transport', return_value=self.transport), \
                patch.object(adapter, 'trial', side_effect=AssertionError('Do not launch or requalify an original job')):
            recovered = adapter.execute(self.config, 'collect', dict(self.request, action_id='explicit-collect'))
        self.assertEqual(recovered['evaluation']['run_log']['status'], 'completed')
        self.assertEqual((self.folder / 'run.log').read_bytes(), full)
        self.assertEqual(adapter.execute(self.config, 'check', self.request), before)
        for name, data in sealed.items(): self.assertEqual((self.folder / name).read_bytes(), data)
        self.assertNotIn(npu_trial.LAUNCH, self.calls)

    def test_log_only_recovery_rejects_changed_original_result_or_handle(self):
        originals = {path: path.read_bytes() for path in (self.remote / 'launch.json', self.remote / 'job/result.json')}
        for kind in ('handle', 'result'):
            with self.subTest(kind=kind):
                for path, data in originals.items(): path.write_bytes(data)
                if kind == 'handle': npu_trial.save(self.remote / 'launch.json', dict(self.handle, pid=1))
                else: npu_trial.save(self.remote / 'job/result.json', dict(self.result, correct=False))
                with patch.object(adapter, 'transport', return_value=self.transport), \
                        patch.object(adapter, 'trial', side_effect=AssertionError('Do not launch')), \
                        self.assertRaisesRegex(ValueError, 'result or handle changed'):
                    adapter.execute(self.config, 'collect', self.request)
                self.assertEqual((self.folder / 'run.log').read_bytes(), self.initial)
        self.assertNotIn(npu_trial.LAUNCH, self.calls)

    def test_log_recovery_timeout_retains_partial_bytes_and_original_terminal_status(self):
        def interrupted(*args, **kwargs): raise TimeoutError('Log-only observation timed out')
        with patch.object(adapter, 'transport', return_value=interrupted):
            result = adapter.execute(self.config, 'collect', self.request)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['evaluation']['run_log']['status'], 'partial')
        self.assertIn('timed out', result['evaluation']['run_log']['recovery_error'])
        self.assertEqual((self.folder / 'run.log').read_bytes(), self.initial)

    def test_runtime_failure_reports_actual_build_phase_without_granting_correctness(self):
        for built in (True, False):
            with self.subTest(build_completed=built):
                result = dict(self.result, status='failed', correct=False, build_completed=built)
                raw = (json.dumps(result, indent=2, allow_nan=False) + '\n').encode()
                digest = hashlib.sha256(raw).hexdigest()
                summary = json.loads((self.folder / 'result.json').read_text())
                observation = json.loads((self.folder / 'observation.json').read_text())
                npu_trial.save(self.folder / 'device-result.json', result)
                npu_trial.save(self.folder / 'result.json', dict(summary, status='failed', correct=False, remote_result_sha256=digest))
                npu_trial.save(self.folder / 'observation.json', dict(observation, result=result, result_sha256=digest))
                checked = adapter.execute(self.config, 'check', self.request)
                self.assertEqual(checked['checks'], {'build': built, 'semantics': False, 'full': False})


class PartialDiagnosticProjectionTests(diagnostic_fixture.OriginalNpuDiagnosticFixture):
    def test_failed_partial_IR_keeps_valid_output_and_failed_attempt_evidence(self):
        report = self.artifact('ir')
        failed_log = self.job / 'recipe-failed.log'; failed_log.write_bytes(b'compiler rejected requested IR emission\n')
        receipt = self.job / 'recipe.json'; receipt.write_text('{"returncode":1}\n')
        attempts = {path.name: adapter.sha(path) for path in (failed_log, receipt)}
        report.update(status='failed', partial=True, reason='One original recipe failed', attempt_evidence=attempts)
        self.result['diagnostics']['reports'] = [report]; self.seal_result()
        result, output = self.project('ir')
        self.assertEqual(result['status'], 'failed'); self.assertTrue(result['partial'])
        self.assertEqual(result['attempt_evidence'], attempts)
        self.assertEqual(result['original_artifacts'], {**report['artifacts'], **attempts})
        self.assertEqual(len(result['artifacts']), 3)
        self.assertFalse(result['artifact_index_truncated'])
        for name, expected in result['artifacts'].items(): self.assertEqual(adapter.sha(output / name), expected)
        self.assertEqual({row['path'] for row in self.reads()}, set(result['original_artifacts']))


if __name__ == '__main__': unittest.main()
