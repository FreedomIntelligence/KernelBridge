"""Replay an explicitly supplied original failed report without remote execution."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples/scatter'))
import npu_trial
import npu_pipeline_driver as adapter


class OriginalFailedBenchmarkReplayTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('KERNELBRIDGE_FAILED_BENCHMARK_ATTEMPT'),
                         'Supply an original failed benchmark attempt for read-only replay')
    def test_original_compile_failure_preserves_report_log_and_readable_first_error(self):
        original = Path(os.environ['KERNELBRIDGE_FAILED_BENCHMARK_ATTEMPT']).resolve(strict=True)
        observation_raw = (original / 'observation.json').read_bytes()
        observation = json.loads(observation_raw)
        raw_result = (json.dumps(observation['result'], indent=2, allow_nan=False) + '\n').encode()
        digest = hashlib.sha256(raw_result).hexdigest()
        self.assertEqual(digest, observation['result_sha256'])
        self.assertEqual(observation['result']['status'], 'failed')
        self.assertIs(observation['result']['correct'], False)
        self.assertNotEqual(observation['execution_exit']['returncode'], 0)
        raw_log = (original / 'run.log').read_bytes()
        self.assertEqual(hashlib.sha256(raw_log).hexdigest(), observation['log']['sha256'])
        pinned = {p: adapter.sha(p) for p in original.iterdir() if p.is_file()}
        with tempfile.TemporaryDirectory(prefix='native-failed-replay-') as temporary:
            root = Path(temporary).resolve(); folder = root / 'attempts/attempt-0001'; folder.mkdir(parents=True)
            for name in ('candidate.py', 'intent.json', 'launch.json', 'execution-exit.json',
                         'observed-launch.json', 'observed-execution-request.json'):
                if (original / name).exists(): shutil.copyfile(original / name, folder / name)
            config = json.loads((original.parents[1] / 'trial-config.json').read_bytes())
            config.update(state=str(folder.parent), candidate=str(folder / 'candidate.py'), wait_seconds=0)
            calls = []
            def offline(argv, stdin=b'', timeout=45):
                calls.append(argv[3])
                if argv[3] == npu_trial.COLLECT:
                    self.assertEqual(argv[4], observation['handle']['attempt'])
                    return {'returncode': 0, 'stdout': observation_raw, 'stderr': b''}
                if argv[3] == npu_trial.READ_RUN_LOG:
                    request = json.loads(stdin)
                    self.assertEqual(request['result_sha256'], digest)
                    self.assertEqual(request['handle'], observation['handle'])
                    self.assertEqual(request['source_sha256'], observation['result']['source_sha256'])
                    self.assertEqual(request['execution_exit_sha256'], observation['execution_exit_sha256'])
                    data = raw_log[request['offset']:request['offset'] + request['limit']]
                    page = {'offset': request['offset'], 'bytes': len(data),
                            'sha256': hashlib.sha256(data).hexdigest(), 'data': base64.b64encode(data).decode()}
                    return {'returncode': 0, 'stdout': json.dumps(page).encode(), 'stderr': b''}
                if argv[3] == adapter.READ_ARTIFACT:
                    raise OSError('Offline replay has only already retained local original artifacts')
                raise AssertionError('No new native work or transport may execute in original replay')
            outcome = npu_trial.collect(config, folder, offline)
            self.assertEqual(outcome['status'], 'completed', outcome)
            self.assertEqual(outcome['data']['result_origin'], 'benchmark')
            self.assertEqual(outcome['data']['remote_result_sha256'], digest)
            self.assertEqual((folder / 'device-result.json').read_bytes(), raw_result)
            self.assertEqual((folder / 'run.log').read_bytes(), raw_log)
            self.assertFalse((folder / 'execution-failure.json').exists())

            contract = copy.deepcopy(config['contract'])
            oracle_root = root / 'oracle'; oracle_root.mkdir()
            oracle_files = {}
            for name, path in contract['oracle']['files'].items():
                target = oracle_root / name; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(Path(path).read_bytes()); oracle_files[name] = adapter.sha(target)
            contract['oracle']['files'] = {'root': str(oracle_root), 'files': oracle_files,
                                           'sha256': adapter.manifest_sha(oracle_files)}
            source_files = {'candidate.py': adapter.sha(folder / 'candidate.py')}
            source = {'root': str(folder), 'files': source_files, 'sha256': adapter.manifest_sha(source_files)}
            request = {'source': source, 'binding': {'source': source['sha256']}, 'contract': contract,
                       'handle': observation['handle'], 'role': 'E', 'action_id': 'original-failure-offline-replay',
                       'output': str(root / 'projection')}
            with patch.object(adapter, 'transport', return_value=offline), \
                    patch.object(adapter, 'trial', side_effect=AssertionError('Never relaunch original receipt')):
                projected = adapter.execute(config, 'collect', request)
                checked = adapter.execute(config, 'check', dict(request, role='F'))
            self.assertEqual(projected['status'], 'completed')
            self.assertEqual(projected['job_status'], 'failed')
            self.assertEqual(projected['remote_result_sha256'], digest)
            feedback = projected['evaluation']
            self.assertEqual(feedback['failure_stage'], 'compile')
            self.assertFalse(feedback['build_completed']); self.assertFalse(feedback['correct'])
            self.assertEqual(checked['checks'], dict.fromkeys(('build', 'semantics', 'full'), False))
            first = feedback['first_compiler_diagnostic']
            self.assertIn('OPS_CHECK_NULL_WITH_CONTEXT', first['text'])
            self.assertEqual(first['read']['tool'], 'pipeline_read')
            for name in ('original-result.json', 'qualified-result.json', 'run.log', 'message.txt', 'traceback.txt'):
                item = feedback['artifacts'][name]
                path = Path(request['output']) / item['path']
                self.assertEqual(adapter.sha(path), item['sha256'])
                self.assertEqual(path.stat().st_size, item['bytes'])
                self.assertEqual(item['read']['tool'], 'pipeline_read')
            self.assertEqual(feedback['artifacts']['original-result.json']['sha256'], digest)
            self.assertEqual(feedback['run_log']['artifact']['sha256'], hashlib.sha256(raw_log).hexdigest())
            self.assertNotIn(npu_trial.LAUNCH, calls)
            if os.environ.get('KERNELBRIDGE_FAILED_REPLAY_OUTPUT'):
                target = Path(os.environ['KERNELBRIDGE_FAILED_REPLAY_OUTPUT'])
                target.mkdir(parents=True, exist_ok=True)
                shutil.copytree(root / 'projection/evaluation', target / 'evaluation', dirs_exist_ok=True)
                (target / 'projected.json').write_text(json.dumps(projected, indent=2) + '\n')
                (target / 'checks.json').write_text(json.dumps(checked, indent=2) + '\n')
        self.assertEqual({p: adapter.sha(p) for p in pinned}, pinned)


if __name__ == '__main__': unittest.main()
