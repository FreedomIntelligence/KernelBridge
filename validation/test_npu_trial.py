"""Receipt-safe NPU trial admission and recovery, without requiring device access."""
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
from ascend_sources import candidate


class NpuTrialTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'candidate.py'
        self.source.write_text(candidate('max', 33))
        self.state = self.root / 'trials'; self.state.mkdir()
        self.config = {'candidate': str(self.source), 'state': str(self.state), 'mode': 'max',
                       'remote_root': '/task', 'wait_seconds': 0, 'poll_seconds': 0}
        self.request(1)
        self.action = {'id': 'original', 'arguments': {'round': 1, 'hypothesis': 'native implementation'}}
        self.calls = []

    def request(self, number):
        (self.state / 'requested.json').write_text(json.dumps({'round': number, 'input_id': 'input'}))

    def remote(self, argv, **kwargs):
        script = argv[3]; self.calls.append(script)
        if script == npu_trial.LAUNCH:
            raise TimeoutError('Acknowledgement lost after remote launch')
        intent = json.loads((self.state / 'round-01/intent.json').read_text())
        result = {'status': 'completed', 'correct': True, 'source_sha256': intent['source_sha256'], 'geomean_speedup': 2.0, 'cases': []}
        return {'returncode': 0, 'stderr': b'', 'stdout': json.dumps({'result': result, 'handle': {'pid': 42},
                'result_sha256': hashlib.sha256(json.dumps(result).encode()).hexdigest(), 'alive': False, 'log_tail': ''}).encode()}

    def test_lost_launch_ack_recovers_and_repeat_does_not_launch(self):
        with patch.object(npu_trial, 'transport', return_value=self.remote):
            first = npu_trial.trial(self.config, self.action)
            again = npu_trial.trial(self.config, self.action)
            recovered = npu_trial.trial(self.config, recover=1)
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(again['data']['original_handle']['pid'], 42)
        self.assertEqual(recovered['data']['source_sha256'], first['data']['source_sha256'])
        self.assertEqual(self.calls.count(npu_trial.LAUNCH), 1)

    def test_unobserved_original_job_blocks_next_round(self):
        with patch.object(npu_trial, 'transport', return_value=lambda *a, **k: (_ for _ in ()).throw(TimeoutError())):
            result = npu_trial.trial(self.config, self.action)
            self.request(2)
            second = npu_trial.trial(self.config, {'id': 'next', 'arguments': {'round': 2, 'hypothesis': 'new'}})
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(second['status'], 'unknown')
        self.assertFalse((self.state / 'round-02').exists())

    def test_comment_and_metadata_changes_are_not_optimization_rounds(self):
        with patch.object(npu_trial, 'transport', return_value=self.remote):
            npu_trial.trial(self.config, self.action)
            parsed = npu_trial.sources(self.source)
            parsed['kernel_src'] = '// changed comment\n' + parsed['kernel_src'].replace('uint32_t', 'uint32_t  ')
            parsed['model_src'] += '\n# changed wrapper\n'
            self.source.write_text('\n'.join(f'{name} = {value!r}' for name, value in parsed.items()))
            self.request(2)
            with self.assertRaisesRegex(ValueError, 'Native implementation already evaluated'):
                npu_trial.trial(self.config, {'id': 'next', 'arguments': {'round': 2, 'hypothesis': 'comments'}})
        self.assertEqual(self.calls.count(npu_trial.LAUNCH), 1)

    def test_cpu_fallback_invalidates_performance_receipt(self):
        def fallback(*args, **kwargs):
            reply = self.remote(*args, **kwargs)
            observation = json.loads(reply['stdout']); observation['cpu_fallback'] = True
            reply['stdout'] = json.dumps(observation).encode(); return reply
        with patch.object(npu_trial, 'transport', return_value=fallback):
            result = npu_trial.trial(self.config, self.action)
        self.assertFalse(result['data']['correct'])
        self.assertEqual(result['data']['status'], 'failed')

    def test_unmodified_initial_candidate_cannot_start_round_one(self):
        initial = self.root / 'initial.py'; initial.write_bytes(self.source.read_bytes())
        self.config['initial_candidate'] = str(initial)
        with patch.object(npu_trial, 'transport', return_value=self.remote):
            with self.assertRaisesRegex(ValueError, 'Unmodified initial implementation'):
                npu_trial.trial(self.config, self.action)
        self.assertEqual(self.calls, [])

    def test_full_log_fallback_scan_survives_long_tail_and_chunk_boundary(self):
        folder = self.root / 'remote-observation'; folder.mkdir()
        for marker in (b'fall back to run on the CPU', b'npu_cpu_fallback'):
            for prefix in (b'', b'x' * (65536 - len(marker) // 2)):
                with self.subTest(marker=marker, prefix_length=len(prefix)):
                    (folder / 'run.log').write_bytes(prefix + marker + b'\n' + b'completed build and profile\n' * 12000)
                    output = subprocess.check_output([sys.executable, '-B', '-c', npu_trial.COLLECT, str(folder)])
                    observation = json.loads(output)
                    self.assertTrue(observation['cpu_fallback'])
                    self.assertNotIn(marker.decode(), observation['log_tail'])
                    self.assertLessEqual(len(observation['log_tail'].encode()), 12000)
        (folder / 'run.log').write_bytes(b'clean native execution\n' * 12000)
        observation = json.loads(subprocess.check_output([sys.executable, '-B', '-c', npu_trial.COLLECT, str(folder)]))
        self.assertFalse(observation['cpu_fallback'])


if __name__ == '__main__': unittest.main()
