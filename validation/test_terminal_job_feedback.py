"""Terminal E feedback using original local receipts; no device/model/transport.

Optional KERNELBRIDGE_TERMINAL_RECEIPT_TRIALS is a JSON list of existing trial
directories. Those inputs are read only, copied to temporary replay workspaces,
and never launched or collected over a transport.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

import test_npu_pipeline_driver as fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/operator_pipeline'))
from pipeline import Pipeline, main
for path in [*ROOT.glob('source/packages/*/src'), ROOT / 'source/apps/worker/src']:
    sys.path.insert(0, str(path))
from kernelcat_worker.application_pipeline_acceptance import collect_pipeline_acceptance

adapter = fixture.adapter


class TerminalJobFeedbackTests(unittest.TestCase):
    def setUp(self):
        fixture.NpuPipelineDriverTest.setUp(self)
        self.actions = {}
        self.result['message'] = 'compiler preamble\nexample.cpp:3: fatal error: missing_tiling.h\nfull retained error tail\n'
        self.result['traceback'] = 'original traceback bytes\n'
        self.result['diagnostics'] = {'required': True, 'satisfied': False, 'reports': [
            {'kind': 'ir', 'status': 'unavailable', 'reason': 'No original compiler recipe', 'attempt_evidence': {}},
            {'kind': 'profiler', 'status': 'unavailable', 'reason': 'Original kernel did not build'}]}
        self.seal('failed')

    def seal(self, status):
        self.result.update(status=status, correct=status == 'completed')
        raw = (json.dumps(self.result, indent=2, allow_nan=False) + '\n').encode()
        digest = hashlib.sha256(raw).hexdigest()
        for name, value in [('device-result.json', self.result),
                            ('result.json', {'status': status, 'correct': self.result['correct'], 'remote_result_sha256': digest,
                                             'original_handle': self.handle, 'remote_attempt': '/not-contacted/original'}),
                            ('observation.json', {'result': self.result, 'result_sha256': digest, 'cpu_fallback': False, 'log_tail': ''})]:
            (self.folder / name).write_text(json.dumps(value))

    def pipeline(self, response=None):
        config = copy.deepcopy(self.config)
        config['state'] = str(self.folder.parent)
        config_path = self.root / ('adapter-' + uuid.uuid4().hex + '.json'); config_path.write_text(json.dumps(config))
        driver = self.root / ('offline-driver-' + uuid.uuid4().hex + '.py')
        driver.write_text('import json,sys\nfrom pathlib import Path\n'
            + 'sys.path.insert(0,' + repr(str(ROOT / 'examples/scatter')) + ')\n'
            + 'import npu_pipeline_driver as adapter\n'
            + 'def forbidden(*args, **kwargs): raise AssertionError("Offline replay must not use trial or transport")\n'
            + 'adapter.trial=adapter.transport=adapter.invoke_remote=forbidden\n'
            + 'request=json.load(sys.stdin)\n'
            + ('result=' + repr(response) + '\n' if response is not None else
               'result=adapter.execute(json.loads(Path(sys.argv[1]).read_text()),"collect",request)\n')
            + 'print(json.dumps(result))\n')
        cfg = {'state': str(self.root / ('pipeline-' + uuid.uuid4().hex)),
               'read_roots': [str(self.root), *self.config['contract']['oracle']['files'].values()],
               'drivers': {name: {'kind': kind, 'argv': [sys.executable, '-B', str(driver), str(config_path)],
                   'files': {str(p): adapter.sha(p) for p in (driver, config_path, ROOT / 'examples/scatter/npu_pipeline_driver.py')}}
                   for name, kind in [('original-launch', 'launch'), ('original-collect', 'collect'), ('original-check', 'check')]}}
        pipeline = Pipeline(cfg)
        self.call(pipeline, 'pipeline_bind', contract=self.config['contract'])
        self.call(pipeline, 'pipeline_plan', stage='B', record={'scope': 'offline existing receipt'})
        self.call(pipeline, 'pipeline_plan', stage='C', record={'hypothesis': 'retained original hypothesis'})
        self.call(pipeline, 'pipeline_freeze', files={'candidate.py': str(self.folder / 'candidate.py')}, origin={'scope': 'offline replay'})
        return pipeline

    def call(self, pipeline, tool, failed=False, **arguments):
        action = {'id': uuid.uuid4().hex, 'task': 'offline-task', 'tool': tool, 'arguments': arguments}
        result = pipeline.apply(action)
        self.assertEqual(result['status'], 'failed' if failed else 'completed', result)
        self.actions[action['id']] = {'action': action, 'terminal': True, 'outcome': result}
        pipeline.chain = Pipeline(pipeline.config).chain
        return result['data']

    def read_all(self, pipeline, artifact, *, limit=16384, encodings=None):
        data = bytearray()
        args = dict(artifact['read']['arguments'], offset=0, limit=limit)
        while True:
            reply = main(pipeline.config, {'id': uuid.uuid4().hex, 'tool': 'pipeline_read', 'arguments': args})
            self.assertEqual(reply['status'], 'completed', reply)
            page = reply['data']
            self.assertEqual(page['offset'], len(data))
            self.assertEqual(page['size_bytes'], artifact['bytes'])
            self.assertEqual(page['sha256'], artifact['sha256'])
            self.assertIn(page['encoding'], ('utf-8', 'base64'))
            chunk = (base64.b64decode(page['text'], validate=True) if page['encoding'] == 'base64'
                     else page['text'].encode('utf-8'))
            data.extend(chunk)
            self.assertEqual(page['next_offset'], len(data))
            if encodings is not None: encodings.append(page['encoding'])
            if page['eof']:
                self.assertIsNone(page['next_read'])
                break
            self.assertTrue(chunk, 'a nonterminal page must advance')
            self.assertEqual(page['next_read']['tool'], 'pipeline_read')
            args = page['next_read']['arguments']
            self.assertEqual(args['offset'], len(data))
        self.assertEqual(len(data), artifact['bytes'])
        self.assertEqual(hashlib.sha256(data).hexdigest(), artifact['sha256'])
        return bytes(data)

    def retain_run_log(self, data, *, complete=True):
        """Install a previously collected original log; projection must stay offline."""
        observation_path = self.folder / 'observation.json'
        observation = json.loads(observation_path.read_text())
        digest = hashlib.sha256(data).hexdigest()
        metadata = {'path': 'run.log', 'bytes': len(data), 'sha256': digest,
                    'identity': [1, 2, 3, len(data)], 'stable': True, 'complete': complete}
        observation.update(log=metadata, log_tail=data[-12000:].decode(errors='replace'))
        observation_path.write_text(json.dumps(observation))
        record = {'status': 'completed' if complete else 'partial', 'complete': complete,
                  'path': 'run.log', 'bytes': len(data), 'sha256': digest,
                  'source_sha256': self.result['source_sha256'],
                  'remote_result_sha256': observation['result_sha256'],
                  'original_handle': self.handle, 'remote_attempt': '/not-contacted/original',
                  'observed': copy.deepcopy(metadata)}
        if not complete: record['reason'] = 'All observed bytes retained; original process was still alive.'
        (self.folder / 'run.log').write_bytes(data)
        (self.folder / 'run-log.json').write_text(json.dumps(record))
        return record

    def test_full_native_log_keeps_runtime_error_before_noise_and_lossless_pages(self):
        error = '[ERROR] original native launch failed with ERR507035'
        prefix = ('native initialization\n' + error + '\n').encode()
        # Put a three-byte character across the first requested page boundary.
        prefix += b' ' * (4095 - len(prefix))
        noise = ('\n' + json.dumps({'noise_calibration': ['eager sample'] * 2000}) + '\n').encode()
        for content, expected_encoding in ((prefix + '中文日志'.encode() + noise, 'utf-8'),
                                           (prefix + b'\xff\x00\x80' + noise, 'base64')):
            with self.subTest(encoding=expected_encoding):
                self.result['message'] = 'The Inner error is reported as above.'
                self.result['traceback'] = 'RuntimeError: generic asynchronous wrapper error\n'
                self.seal('failed'); self.retain_run_log(content)
                observation = json.loads((self.folder / 'observation.json').read_text())
                self.assertNotIn(error, observation['log_tail'])
                pipeline = self.pipeline()
                reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch')
                evaluation = reply['result']['result']['evaluation']
                first = evaluation['first_runtime_error']
                self.assertIsNone(evaluation['first_compiler_diagnostic'])
                self.assertEqual((first['text'], first['line'], first['artifact']), (error, 2, 'run.log'))
                self.assertEqual(first['read'], evaluation['artifacts']['run.log']['read'])
                self.assertEqual(evaluation['error_excerpt'], error)
                self.assertEqual(evaluation['run_log']['status'], 'completed')
                self.assertTrue(evaluation['run_log']['complete'])
                self.assertEqual(reply['workflow']['next_actions'][0]['stage'], 'C')
                encodings = []
                retained = self.read_all(pipeline, evaluation['artifacts']['run.log'], limit=4096, encodings=encodings)
                self.assertEqual(retained, content)
                self.assertIn(expected_encoding, encodings)
                self.assertGreater(len(encodings), 1)
                if expected_encoding == 'utf-8': self.assertEqual(set(encodings), {'utf-8'})

    def test_full_native_log_compiler_error_and_location_precede_summary_noise(self):
        location = '/original/job/op_host/example.cpp:27:9:'
        error = '  fatal error: original generated include missing'
        content = ('frame #0: c10::Error::Error()\n' + location + '\n' + error + '\n'
                   + json.dumps({'noise_calibration': ['unchanged eager observation'] * 2000}) + '\n').encode()
        self.result['message'] = 'Generic build wrapper failed.'
        self.result['traceback'] = 'RuntimeError: wrapper only\n'
        self.seal('failed'); self.retain_run_log(content)
        pipeline = self.pipeline()
        reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch')
        evaluation = reply['result']['result']['evaluation']; first = evaluation['first_compiler_diagnostic']
        self.assertEqual((first['text'], first['line'], first['artifact']), (error, 3, 'run.log'))
        self.assertEqual(first['context_before'], [{'text': location, 'line': 2, 'artifact': 'run.log', 'truncated': False}])
        self.assertIsNone(evaluation['first_runtime_error'])
        self.assertEqual(self.read_all(pipeline, evaluation['artifacts']['run.log']), content)

    def test_retained_native_log_tampering_cannot_borrow_original_job_identity(self):
        content = b'[ERROR] original native failure\n' + b'unchanged output\n' * 1000
        variants = [('source_sha256', '0' * 64), ('remote_result_sha256', '0' * 64),
                    ('remote_attempt', '/different/job'), ('original_handle', dict(self.handle, pid=9876)),
                    ('sha256', '0' * 64), ('bytes', len(content) + 1), ('complete', False)]
        for field, value in variants:
            with self.subTest(field=field):
                record = self.retain_run_log(content); record[field] = value
                (self.folder / 'run-log.json').write_text(json.dumps(record))
                pipeline = self.pipeline()
                reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch', failed=True)
                stderr = next(item for item in reply['artifacts']['files'] if item['path'] == 'driver/stderr.log')
                error = self.read_all(pipeline, dict(stderr, bytes=stderr['size_bytes'])).decode()
                self.assertRegex(error, 'Original (?:run log binding|retained run log identity or length) changed')
                self.assertEqual(reply['result']['launch_outcome'], 'unknown')
                self.assertEqual(pipeline.state['handles'], [])
                self.assertIsNone(pipeline.state['check']); self.assertIsNone(pipeline.state['pair'])
                repeated = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch', failed=True)
                self.assertIn('Recover the original', repeated['result']['error'])
        for variant in ('observed_hash', 'observed_length', 'file_hash', 'file_length'):
            with self.subTest(variant=variant):
                self.retain_run_log(content)
                if variant.startswith('observed'):
                    path = self.folder / 'observation.json'; observation = json.loads(path.read_text())
                    observation['log']['sha256' if variant == 'observed_hash' else 'bytes'] = ('f' * 64 if variant == 'observed_hash' else len(content) + 1)
                    path.write_text(json.dumps(observation))
                else:
                    (self.folder / 'run.log').write_bytes((b'X' + content[1:]) if variant == 'file_hash' else content + b'X')
                pipeline = self.pipeline()
                reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch', failed=True)
                stderr = next(item for item in reply['artifacts']['files'] if item['path'] == 'driver/stderr.log')
                error = self.read_all(pipeline, dict(stderr, bytes=stderr['size_bytes'])).decode()
                self.assertIn('run log identity or length changed', error)
                self.assertEqual(reply['result']['launch_outcome'], 'unknown')

    def test_partial_native_log_remains_partial_and_legacy_tail_stays_offline(self):
        content = b'[ERROR] original process not yet fully reaped\n'
        self.retain_run_log(content, complete=False)
        pipeline = self.pipeline()
        reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch')
        evaluation = reply['result']['result']['evaluation']
        self.assertEqual(evaluation['run_log']['status'], 'partial')
        self.assertFalse(evaluation['run_log']['complete'])
        self.assertEqual(self.read_all(pipeline, evaluation['artifacts']['run.log']), content)
        (self.folder / 'run.log').unlink(); (self.folder / 'run-log.json').unlink()
        self.seal('failed')
        observation_path = self.folder / 'observation.json'
        observation = json.loads(observation_path.read_text())
        observation['log_tail'] = 'Legacy tail only. 中文\n'
        observation_path.write_text(json.dumps(observation))
        pipeline = self.pipeline()
        reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch')
        evaluation = reply['result']['result']['evaluation']
        self.assertEqual(evaluation['run_log']['status'], 'unavailable')
        self.assertFalse(evaluation['run_log']['complete'])
        self.assertIn('Legacy observation', evaluation['run_log']['reason'])
        self.assertNotIn('run.log', evaluation['artifacts'])
        self.assertEqual(self.read_all(pipeline, evaluation['artifacts']['legacy-log-tail.txt']), observation['log_tail'].encode())
        self.assertEqual(reply['workflow']['next_actions'][0]['stage'], 'C')

    def test_terminal_failure_exposes_first_original_error_links_and_direct_C(self):
        pipeline = self.pipeline()
        reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch')
        result = reply['result']['result']; evaluation = result['evaluation']
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(result['job_status'], 'failed')
        self.assertEqual(evaluation['first_compiler_diagnostic']['text'], 'example.cpp:3: fatal error: missing_tiling.h')
        self.assertEqual(evaluation['first_compiler_diagnostic']['line'], 2)
        self.assertEqual(reply['workflow']['next_actions'][0]['stage'], 'C')
        self.assertTrue(pipeline.state['handles'][0]['collected'])
        self.assertIsNone(pipeline.state['check']); self.assertIsNone(pipeline.state['pair'])
        self.assertEqual([row['available'] for row in evaluation['diagnostics']['reports']], [False, False])
        self.assertEqual(self.read_all(pipeline, evaluation['artifacts']['message.txt']).decode(), self.result['message'])
        original = self.read_all(pipeline, evaluation['artifacts']['original-result.json'])
        self.assertEqual(hashlib.sha256(original).hexdigest(), evaluation['remote_result_sha256'])
        self.assertEqual(json.loads(original), self.result)
        # Editing retained feedback cannot silently replace the original read.
        path = Path(pipeline.config['state']) / reply['run'] / 'driver/evaluation/message.txt'; path.write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'Artifact missing or changed'):
            main(pipeline.config, {'id': uuid.uuid4().hex, 'tool': 'pipeline_read',
                 'arguments': evaluation['artifacts']['message.txt']['read']['arguments']})

    def test_cpp_stack_frames_do_not_hide_later_original_compiler_diagnostics(self):
        stack = 'frame #0: c10::Error::Error(c10::SourceLocation, std::string) + 0x54\n'
        error = '  file.cpp:27:9: error: unknown type name TilingData'
        for field in ('message', 'traceback'):
            with self.subTest(field=field):
                self.result['message'] = stack + 'RuntimeError: original native build failed\n'
                self.result['traceback'] = 'Traceback (most recent call last):\n' + stack
                self.result[field] += error + '\n'
                self.seal('failed'); pipeline = self.pipeline()
                reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch')
                evaluation = reply['result']['result']['evaluation']; first = evaluation['first_compiler_diagnostic']
                self.assertEqual(first['text'], error); self.assertEqual(first['line'], 3)
                self.assertEqual(first['artifact'], field + '.txt')
                self.assertIsNone(evaluation['first_runtime_error'])
                original = self.read_all(pipeline, evaluation['artifacts'][first['artifact']]).decode()
                self.assertEqual(original.splitlines()[first['line'] - 1], first['text'])
                self.assertEqual(first['read'], evaluation['artifacts'][first['artifact']]['read'])

    def test_runtime_fallback_preserves_original_line_without_claiming_compiler_error(self):
        self.result['message'] = 'frame #0: c10::Error::Error(c10::SourceLocation, std::string) + 0x54\n'
        self.result['traceback'] = ('Traceback (most recent call last):\n'
            '  frame #1: std::RuntimeError::RuntimeError() + 0x28\n'
            'RuntimeError: ACL execution failed with status 507035\n')
        self.seal('failed'); pipeline = self.pipeline()
        reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch')
        evaluation = reply['result']['result']['evaluation']
        self.assertIsNone(evaluation['first_compiler_diagnostic'])
        fallback = evaluation['first_runtime_error']
        self.assertEqual(fallback['text'], 'RuntimeError: ACL execution failed with status 507035')
        self.assertEqual(fallback['line'], 3); self.assertEqual(fallback['artifact'], 'traceback.txt')
        self.assertEqual(evaluation['error_excerpt'], fallback['text'])
        original = self.read_all(pipeline, evaluation['artifacts'][fallback['artifact']]).decode()
        self.assertEqual(original.splitlines()[fallback['line'] - 1], fallback['text'])
        self.assertEqual(fallback['read'], evaluation['artifacts'][fallback['artifact']]['read'])

    def test_error_preview_is_bounded_and_complete_error_and_report_remain_readable(self):
        self.result['message'] = 'prefix\nfile.cpp: fatal error: ' + 'x' * 6000 + '\n' + ('retained tail\n' * 10000)
        self.result['traceback'] = 'retained traceback\n' * 3000
        self.result['diagnostics']['reports'][0] = {
            'kind': 'ir', 'status': 'completed', 'artifacts': {'kernel.ll': {'sha256': 'a' * 64}},
            'reason': 'original receipt reports an emitted artifact'}
        self.seal('failed'); pipeline = self.pipeline()
        reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch')
        self.assertIsInstance(reply['result'], dict)  # bounded preview stays inline
        self.assertLess(len(json.dumps(reply['result']).encode()), 16000)
        evaluation = reply['result']['result']['evaluation']; first = evaluation['first_compiler_diagnostic']
        self.assertEqual(len(first['text']), 2048); self.assertTrue(first['truncated'])
        self.assertEqual(self.read_all(pipeline, evaluation['artifacts']['message.txt']).decode(), self.result['message'])
        self.assertEqual(self.read_all(pipeline, evaluation['artifacts']['traceback.txt']).decode(), self.result['traceback'])
        self.assertEqual([row['available'] for row in evaluation['diagnostics']['reports']], [True, False])
        self.assertIsNone(pipeline.state['check']); self.assertIsNone(pipeline.state['pair'])

    def test_terminal_success_skips_collect_but_cannot_establish_F_or_host_acceptance(self):
        self.seal('completed'); pipeline = self.pipeline()
        reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch')
        self.assertEqual(reply['workflow']['next_actions'][0]['stage'], 'F')
        self.assertIsNone(pipeline.state['check']); self.assertIsNone(pipeline.state['pair'])
        self.call(pipeline, 'pipeline_promote', failed=True)
        commands = []
        store = SimpleNamespace(events=lambda: [], _events=lambda rows: rows, snapshot=lambda: {'actions': self.actions})
        host = SimpleNamespace(evaluator={'files': {}}, owner=SimpleNamespace(gateway=SimpleNamespace(store=store)),
                               task=SimpleNamespace(id='offline-task'), journal=SimpleNamespace(command=lambda *args: commands.append(args)))
        collect_pipeline_acceptance(host, {'evidence': [], 'goal': {'status': 'active'}}, {'tool': 'pipeline_promote'})
        self.assertEqual(commands, [])

    def test_launch_acks_and_explicit_nonterminal_results_still_require_original_collection(self):
        for extra in ({'status': 'completed'}, {'status': 'completed', 'phase': 'running'},
                      {'status': 'completed', 'phase': 'complete', 'job_status': 'unknown'},
                      {'status': 'unknown', 'phase': 'complete', 'job_status': 'failed'}):
            with self.subTest(extra=extra):
                pipeline = self.pipeline({'handle': {'pid': 42}, **extra})
                reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch')
                self.assertNotIn('collected', pipeline.state['handles'][0])
                next_action = reply['workflow']['next_actions'][0]
                self.assertEqual(next_action['arguments']['driver'], 'original-collect')
                self.assertEqual(next_action['arguments']['handle'], {'pid': 42})
                collected = self.call(pipeline, 'pipeline_run', stage='E', driver='original-collect', handle={'pid': 42})
                if extra == {'status': 'completed'}:
                    self.assertTrue(pipeline.state['handles'][0]['collected'])
                    self.assertEqual(collected['workflow']['next_actions'][0]['stage'], 'F')
                else:
                    self.assertNotIn('collected', pipeline.state['handles'][0])
                    self.assertEqual(collected['workflow']['next_actions'][0]['arguments']['driver'], 'original-collect')

    def test_source_or_original_result_tampering_rejects_terminal_projection(self):
        pipeline = self.pipeline()
        observation = self.folder / 'observation.json'
        data = json.loads(observation.read_text()); data['result']['message'] = 'replacement error'; observation.write_text(json.dumps(data))
        reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch', failed=True)
        self.assertEqual(reply['result']['launch_outcome'], 'unknown')
        self.assertEqual(reply['workflow']['next_actions'][0]['arguments']['recover_run'], reply['run'])
        self.assertEqual(pipeline.state['handles'], [])
        repeated = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch', failed=True)
        self.assertIn('Recover the original', repeated['result']['error'])

    def test_native_collect_of_nonterminal_original_result_is_not_complete(self):
        self.seal('running'); output = self.root / 'nonterminal'; output.mkdir()
        with patch.object(adapter, 'transport', side_effect=AssertionError('no transport')):
            with self.assertRaisesRegex(ValueError, 'no terminal result'):
                adapter.execute(self.config, 'collect', dict(self.request, output=str(output), action_id='nonterminal'))

    def test_original_max_and_cube_receipts_offline(self):
        trials = json.loads(os.environ.get('KERNELBRIDGE_TERMINAL_RECEIPT_TRIALS', '[]'))
        if not trials: self.skipTest('Supply existing trial paths for read-only offline replay')
        for name in trials:
            with self.subTest(trial=name):
                trial = Path(name).resolve()
                config = json.loads((trial / 'trial-config.json').read_text())
                attempt = sorted((trial / 'attempts').glob('*/device-result.json'))[0].parent
                # Only temporary copies are writable; original inputs never change.
                originals = {p: adapter.sha(p) for p in attempt.iterdir() if p.is_file()}
                replay = self.root / ('replay-' + uuid.uuid4().hex) / 'attempts/attempt-0001'; replay.mkdir(parents=True)
                for filename in ('candidate.py', 'intent.json', 'device-result.json', 'result.json', 'observation.json'):
                    shutil.copyfile(attempt / filename, replay / filename)
                self.folder = replay; self.config = config; self.result = json.loads((replay / 'device-result.json').read_text())
                pipeline = self.pipeline()
                reply = self.call(pipeline, 'pipeline_run', stage='E', driver='original-launch')
                evaluation = reply['result']['result']['evaluation']
                first = evaluation['first_compiler_diagnostic']
                self.assertIsNotNone(first)
                self.assertIn('fatal error:', first['text'])
                self.assertIn(first['text'], self.result['message'])
                self.assertIsInstance(reply['result'], dict)
                self.assertLess(len(json.dumps(reply['result']).encode()), 16000)
                self.assertEqual(len(first['context_before']), 1)
                location = first['context_before'][0]
                self.assertEqual(location['line'], first['line'] - 1)
                self.assertEqual(location['artifact'], first['artifact'])
                self.assertEqual(location['text'], self.result['message'].splitlines()[location['line'] - 1])
                self.assertIn('/op_host/', location['text']); self.assertFalse(location['truncated'])
                self.assertLessEqual(sum(len(row['text'].encode()) for row in first['context_before']), 1024)
                self.assertEqual(reply['workflow']['next_actions'][0]['stage'], 'C')
                self.assertEqual(self.read_all(pipeline, evaluation['artifacts']['message.txt']).decode(), self.result['message'])
                raw = self.read_all(pipeline, evaluation['artifacts']['original-result.json'])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), evaluation['remote_result_sha256'])
                self.assertTrue(all(adapter.sha(p) == value for p, value in originals.items()))
                print(json.dumps({'offline_original_trial': trial.name, 'first_compiler_diagnostic': first['text'],
                    'location_context': first['context_before'],
                    'next_stage': 'C', 'original_result_sha256': evaluation['remote_result_sha256'],
                    'message_bytes': evaluation['artifacts']['message.txt']['bytes'], 'transport_calls': 0}), flush=True)


if __name__ == '__main__': unittest.main()
