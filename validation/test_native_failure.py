"""A crashed native runner must permit repair without disguising an uncertain job."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'source/packages/executor/src'))
from kernelcat_executor.evaluation.evaluate import run_job
from kernelcat_executor.evaluation.protocols.six_source.config import Request


class NativeFailure(unittest.TestCase):
    def test_reaped_runner_crash_is_failed_with_bounded_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / 'candidate.py'; candidate.write_text('candidate')
            reference = root / 'reference.py'; reference.write_text('reference')
            def crash(argv, cwd, env, log, timeout):
                log.write_text('x' * 40000 + '\nCorrectness: begin 5 trials\ncrash marker\n')
                return -11, None
            request = Request(candidate, reference, root / 'job', 'test', 'ai_core-Ascend910_9382')
            with patch('kernelcat_executor.evaluation.evaluate._execute', side_effect=crash):
                result = run_job(request)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['data']['returncode'], -11)
            self.assertEqual(result['data']['failure_stage'], 'runner_error')
            self.assertIn('crash marker', result['data']['diagnostic'])
            self.assertLessEqual(len(result['data']['diagnostic']), 32768)
            self.assertFalse(result['data']['promoted'])

    def test_timeout_remains_unknown_and_original_job_cannot_be_repeated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / 'candidate.py'; candidate.write_text('candidate')
            reference = root / 'reference.py'; reference.write_text('reference')
            request = Request(candidate, reference, root / 'job', 'test', 'ai_core-Ascend910_9382')
            with patch('kernelcat_executor.evaluation.evaluate._execute', return_value=(None, 'timeout')) as execute:
                result = run_job(request)
                self.assertEqual(result['status'], 'unknown')
                with self.assertRaises(FileExistsError):
                    run_job(request)
                self.assertEqual(execute.call_count, 1)
