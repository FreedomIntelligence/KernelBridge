"""CPU comparison diagnostics and real offline Pipeline receipt projection.

No candidate compiler, model service, remote transport or NPU is used. The
optional persistent fixture records real Pipeline.apply/read replies; JSON wire
roundtrip alone is explicitly not a Pi SDK next-request capture.
"""
import hashlib
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'validation'))
sys.path.insert(0, str(ROOT / 'examples/scatter'))
sys.path.insert(0, str(ROOT / 'source/packages/executor/src'))
import torch
import npu_matmul_benchmark as cube
import npu_benchmark as scatter
from kernelcat_executor.backends.ascend import compiler, runtime
import kernelcat_executor.evaluation.failure_summary as failure_module
from kernelcat_executor.evaluation.failure_summary import summarize_tensor_failure
import test_terminal_job_feedback as original


class TensorFailureSummaryTests(unittest.TestCase):
    def test_exact_comparison_nonfinite_values_keep_original_semantics_and_json(self):
        actual = torch.tensor([1., float('nan'), float('inf'), -float('inf'), 4.])
        expected = torch.tensor([0., float('nan'), float('inf'), float('inf'), 4.])
        self.assertFalse(torch.equal(actual, expected))
        report = summarize_tensor_failure(actual, expected, comparison='equal')
        self.assertEqual(report['mismatch_count'], 3)
        self.assertEqual([item['flat_index'] for item in report['examples']], [0, 1, 3])
        self.assertEqual(report['examples'][1]['actual'], 'nan')
        self.assertEqual(report['examples'][2], {'flat_index': 3, 'actual': '-inf', 'expected': '+inf'})
        json.dumps(report, allow_nan=False)

    def test_allclose_uses_original_tolerance_and_finite_actual_requirement(self):
        actual = torch.tensor([1., 1.019, 1.021, float('inf'), float('nan')], dtype=torch.float16)
        expected = torch.tensor([1., 1., 1., float('inf'), 0.], dtype=torch.float16)
        self.assertFalse(torch.isfinite(actual).all().item() and torch.allclose(actual, expected, atol=0.01, rtol=0.01))
        report = summarize_tensor_failure(actual, expected, comparison='allclose', atol=0.01, rtol=0.01, require_finite=True)
        self.assertEqual(report['mismatch_count'], 3)
        self.assertEqual([item['flat_index'] for item in report['examples']], [2, 3, 4])
        self.assertEqual(report['actual']['dtype'], 'torch.float16')
        self.assertEqual(report['comparison'], {'kind': 'allclose', 'atol': 0.01, 'rtol': 0.01, 'require_finite': True})
        json.dumps(report, allow_nan=False)

    def test_shape_contract_and_non_cpu_are_metadata_only_without_device_transfer(self):
        with patch.object(torch.Tensor, 'cpu', side_effect=AssertionError('No transfer allowed')), \
             patch.object(torch.Tensor, 'to', side_effect=AssertionError('No transfer allowed')), \
             patch.object(torch, 'eq', side_effect=AssertionError('No contents needed')), \
             patch.object(torch, 'isclose', side_effect=AssertionError('No contents needed')):
            changed = summarize_tensor_failure(torch.zeros((2, 1)), torch.zeros(2), comparison='equal')
            contract = summarize_tensor_failure(torch.zeros(2), torch.zeros(2), comparison='contract')
            noncpu = summarize_tensor_failure(torch.empty(2, device='meta'), torch.zeros(2), comparison='equal')
        self.assertEqual(changed['reason'], 'shape_mismatch')
        self.assertEqual(changed['actual']['shape'], [2, 1])
        self.assertEqual(contract['reason'], 'output_contract_failed')
        self.assertIn('no device transfer', noncpu['reason'])
        for report in (changed, contract, noncpu):
            self.assertIsNone(report['mismatch_count'])
            self.assertEqual(report['examples'], [])

    def test_chunked_count_and_first_four_samples_have_bounded_index_storage(self):
        size = 2 * 65536 + 9
        actual, expected = torch.ones(size), torch.zeros(size)
        observed_sizes, counted_sizes = [], []
        original_nonzero, original_count = torch.nonzero, torch.count_nonzero
        def nonzero(value, **kwargs):
            observed_sizes.append(value.numel())
            self.assertLessEqual(value.numel(), 65536)
            return original_nonzero(value, **kwargs)
        def count(value):
            counted_sizes.append(value.numel())
            self.assertLessEqual(value.numel(), 65536)
            return original_count(value)
        with patch.object(torch, 'nonzero', side_effect=nonzero), patch.object(torch, 'count_nonzero', side_effect=count):
            report = summarize_tensor_failure(actual, expected, comparison='equal')
        self.assertEqual(report['mismatch_count'], size)
        self.assertEqual([item['flat_index'] for item in report['examples']], [0, 1, 2, 3])
        self.assertEqual(observed_sizes, [65536])
        self.assertEqual(counted_sizes, [65536, 65536, 9])
        actual.zero_(); actual[-3:] = 1
        report = summarize_tensor_failure(actual, expected, comparison='equal')
        self.assertEqual(report['mismatch_count'], 3)
        self.assertEqual([item['flat_index'] for item in report['examples']], [size - 3, size - 2, size - 1])

    def test_diagnostic_error_cannot_produce_a_partial_complete_count(self):
        with patch.object(torch, 'eq', side_effect=RuntimeError('fixture diagnosis failed')):
            report = summarize_tensor_failure(torch.zeros(3), torch.ones(3), comparison='equal')
        self.assertEqual(report['diagnostic_status'], 'unavailable')
        self.assertIsNone(report['mismatch_count'])
        self.assertEqual(report['diagnostic_error'], {'type': 'RuntimeError', 'message': 'fixture diagnosis failed'})

    def case(self):
        value = torch.ones((128, 128), dtype=torch.float16)
        return {'a': value.clone(), 'b': value.clone(), 'input_a': value.clone(), 'input_b': value.clone(),
                'oracle': value, 'seed': 17, 'input_sha256': 'a' * 64}

    def test_original_cube_comparison_runs_candidate_once_and_reports_cpu_values(self):
        case = self.case(); calls, reports = [], []
        def candidate(a, b):
            calls.append((a, b))
            return torch.zeros_like(case['oracle'])
        def failure(observed_case, cpu, comparison):
            self.assertIs(observed_case, case)
            self.assertEqual(cpu.device.type, 'cpu')
            reports.append(summarize_tensor_failure(cpu, case['oracle'], comparison=comparison,
                                                   atol=0.01, rtol=0.01, require_finite=True))
        with patch.object(torch, 'npu', SimpleNamespace(synchronize=lambda **kw: None), create=True):
            with self.assertRaisesRegex(ValueError, 'FP32 oracle mismatch for seed 17'):
                cube.check(candidate, [case], torch, torch.device('cpu'), on_failure=failure)
        self.assertEqual(len(calls), 1)
        self.assertEqual(reports[0]['mismatch_count'], 128 * 128)

    def test_cube_callback_error_preserves_original_failure_and_success_never_calls_it(self):
        case = self.case(); calls = []
        def broken_callback(*args):
            raise RuntimeError('diagnostic callback must not replace comparator failure')
        def candidate(a, b):
            calls.append(True)
            return torch.zeros_like(case['oracle'])
        with patch.object(torch, 'npu', SimpleNamespace(synchronize=lambda **kw: None), create=True):
            with self.assertRaisesRegex(ValueError, '^FP32 oracle mismatch for seed 17$'):
                cube.check(candidate, [case], torch, torch.device('cpu'), on_failure=broken_callback)
            checked = cube.check(lambda a, b: case['oracle'], [case], torch, torch.device('cpu'), on_failure=broken_callback)
        self.assertEqual(calls, [True])
        self.assertTrue(checked[0]['correct'])

    def test_cube_contract_failure_does_not_copy_or_inspect_output_elements(self):
        case = self.case(); calls, reports = [], []
        def candidate(a, b):
            calls.append(True)
            return torch.zeros((127, 128), dtype=torch.float16)
        with patch.object(torch, 'npu', SimpleNamespace(synchronize=lambda **kw: None), create=True), \
             patch.object(torch.Tensor, 'cpu', side_effect=AssertionError('Must not transfer contract failure')):
            with self.assertRaisesRegex(ValueError, '^Output contract failed for seed 17$'):
                cube.check(candidate, [case], torch, torch.device('cpu'), on_failure=lambda c, actual, comparison:
                           reports.append(summarize_tensor_failure(actual, c['oracle'], comparison=comparison)))
        self.assertEqual(calls, [True])
        self.assertEqual(reports[0]['reason'], 'shape_mismatch')

    def test_scatter_driver_reuses_observed_cpu_result_and_preserves_failure_if_summary_raises(self):
        for summary_raises in (False, True):
            with self.subTest(summary_raises=summary_raises), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve(); candidate = root / 'candidate.py'
                candidate.write_text(''.join(f'{name} = "protocol fixture, never compiled"\n' for name in sorted(scatter.FIELDS)))
                args = SimpleNamespace(candidate=candidate, output=root / 'job', mode='max', soc='ai_core-Ascend910_9382',
                                       device=1, warmup=3, repeats=10, pairs=3, profile=False)
                expected = torch.ones((3, 7)); actual = expected.clone(); actual[1, 2] = 0
                case = {'id': 'fixture-case', 'input_sha256': 'b' * 64, 'src': torch.ones((17, 7)),
                        'index': torch.zeros(17, dtype=torch.int64), 'expected': expected}
                calls = []
                class Candidate:
                    def eval(self): return self
                    def __call__(self, *inputs):
                        calls.append(inputs)
                        return actual
                instance = SimpleNamespace(device=torch.device('cpu'), context={'ModelNew': Candidate},
                                           load=lambda *args: None, close=lambda: None)
                torch_npu = ModuleType('torch_npu'); torch_npu.__version__ = 'CPU-fixture-only'
                built = SimpleNamespace(files=[], source_digest='fixture-source', target='fixture-target')
                with ExitStack() as stack:
                    stack.enter_context(patch.dict(sys.modules, {'torch_npu': torch_npu}))
                    stack.enter_context(patch.object(torch, 'npu', SimpleNamespace(
                        synchronize=lambda: None, get_device_name=lambda _: 'CPU fixture'), create=True))
                    compiled = stack.enter_context(patch.object(compiler, 'compile_sources', return_value=built))
                    stack.enter_context(patch.object(compiler, 'collect_build_diagnostics', return_value={'artifacts': {}}))
                    stack.enter_context(patch.object(runtime, 'AscendRuntime', return_value=instance))
                    stack.enter_context(patch.object(scatter, 'audit_wrapper', return_value={'fixture': True}))
                    stack.enter_context(patch.object(scatter, 'audit_native_binding', return_value={'fixture': True}))
                    stack.enter_context(patch.object(scatter, 'suite', return_value=[case]))
                    eager = stack.enter_context(patch.object(scatter, 'scatter_eager', return_value=expected))
                    stack.enter_context(patch.object(scatter, 'measure', return_value=1))
                    stack.enter_context(patch.object(scatter, 'collect_ir', return_value=[]))
                    if summary_raises:
                        stack.enter_context(patch.object(failure_module, 'summarize_tensor_failure',
                                                         side_effect=RuntimeError('summary fixture failure')))
                    with redirect_stdout(io.StringIO()):
                        self.assertFalse(scatter.run(args))
                result = json.loads((args.output / 'result.json').read_text())
                self.assertEqual(len(calls), 1, result)
                self.assertEqual(eager.call_count, 2)
                compiled.assert_called_once()
                self.assertEqual((result['status'], result['failure_stage'], result['message']),
                                 ('failed', 'correctness', 'Candidate correctness failed: fixture-case'))
                report = result['correctness_failure']
                self.assertEqual(report['binding']['candidate_sha256'], result['source_sha256'])
                self.assertEqual(report['binding']['inputs']['src']['shape'], [17, 7])
                if summary_raises:
                    self.assertEqual(report['diagnostic_error']['message'], 'summary fixture failure')
                else:
                    self.assertEqual(report['mismatch_count'], 1)
                    self.assertEqual(report['examples'], [{'flat_index': 9, 'actual': 0, 'expected': 1}])


class TensorFailurePipelineTests(unittest.TestCase):
    def setUp(self):
        self.h = original.TerminalJobFeedbackTests(methodName='runTest')
        self.h.setUp(); self.addCleanup(self.h.doCleanups)
        expected = torch.ones((3, 7)); actual = expected.clone(); actual[1, 2] = 0
        self.report = summarize_tensor_failure(actual, expected, comparison='equal', binding={
            'case_id': 'case-one', 'input_sha256': 'a' * 64,
            'candidate_sha256': self.h.result['source_sha256'], 'role': 'candidate',
            'inputs': {'values': {'shape': [17, 7], 'dtype': 'torch.float32'}}})
        self.h.result.update(cases=[], build_completed=True, failure_stage='correctness',
                             message='Candidate correctness failed: case-one',
                             traceback='ValueError: Candidate correctness failed: case-one\n',
                             correctness_failure=self.report)
        self.h.seal('failed')

    def test_actual_pipeline_summary_and_composite_feedback_keep_original_bound_report(self):
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.h.folder.iterdir() if path.is_file()}
        pipeline = self.h.pipeline()
        pipeline.config['evaluation_enabled'] = True
        # Existing replay fixture uses collect for every role. F is pointed to
        # the same real adapter's read-only check operation in this fixture.
        check = pipeline.config['drivers']['original-check']
        driver = Path(check['argv'][2]); check_driver = self.h.root / 'offline-check.py'
        check_driver.write_text(driver.read_text().replace('"collect",request)', '"check",request)'))
        check['argv'] = [*check['argv'][:2], str(check_driver), *check['argv'][3:]]
        check['files'] = {**check['files'], str(check_driver): original.adapter.sha(check_driver)}
        reply = self.h.call(pipeline, 'pipeline_run', stage='E', driver='original-launch')
        feedback = reply['summary']['evaluation']
        self.assertEqual(feedback['correctness_failure'], self.report)
        self.assertEqual(feedback['failure_stage'], 'correctness')
        self.assertEqual(feedback['native_candidate_sha256'], self.report['binding']['candidate_sha256'])
        artifact = feedback['artifacts']['original-result.json']
        raw = self.h.read_all(pipeline, artifact)
        self.assertEqual(json.loads(raw)['correctness_failure'], self.report)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), feedback['remote_result_sha256'])
        composite = self.h.call(pipeline, 'pipeline_evaluate', files={'candidate.py': str(self.h.folder / 'candidate.py')},
                                origin={'scope': 'CPU-only original receipt replay'})
        summary = composite['evaluation_summary']
        self.assertEqual(summary['evaluation_status'], 'failed')
        self.assertEqual(summary['feedback']['correctness_failure'], self.report)
        self.assertEqual(composite['result']['check_receipt']['result']['correctness_failure'], self.report)
        self.assertFalse(original.Pipeline(pipeline.config).state['check']['passed'])
        self.assertIsNone(original.Pipeline(pipeline.config).state['incumbent'])
        read_reply = original.main(pipeline.config, {'id': uuid.uuid4().hex, **artifact['read']})
        self.assertEqual(read_reply['data']['sha256'], artifact['sha256'])
        # A protocol JSON roundtrip, not a claim of a Pi next-request capture.
        fixture = {'schema_version': 'kernelbridge.tensor-failure-pipeline-fixture/v1',
                   'scope': 'CPU-only Pipeline.apply and pipeline_read; JSON wire roundtrip is not Pi request capture',
                   'pipeline_reply': {'status': 'completed', 'data': reply},
                   'composite_reply': {'status': 'completed', 'data': composite}, 'read_reply': read_reply}
        wire = {'role': 'tool', 'tool_call_id': 'fixture-original-pipeline', 'content': json.dumps(fixture['pipeline_reply'])}
        self.assertEqual(json.loads(wire['content'])['data']['summary']['evaluation']['correctness_failure'], self.report)
        self.assertTrue(all(hashlib.sha256(path.read_bytes()).hexdigest() == value for path, value in before.items()))
        destination = os.environ.get('KERNELBRIDGE_TENSOR_FAILURE_FIXTURE')
        if destination:
            path = Path(destination); path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(fixture, indent=2, allow_nan=False) + '\n')

    def test_projector_caps_oversized_or_misbound_report_without_altering_failure(self):
        for update in ({'padding': 'x' * 8193}, {'binding': {'candidate_sha256': '0' * 64}}):
            with self.subTest(update=list(update)):
                result = {**self.h.result, 'correctness_failure': {**self.report, **update}}
                before = json.dumps(result, sort_keys=True)
                projected = original.adapter.correctness_feedback(result)
                self.assertEqual(projected['correctness_failure']['diagnostic_status'], 'unavailable')
                self.assertEqual(json.dumps(result, sort_keys=True), before)
                self.assertEqual(result['message'], 'Candidate correctness failed: case-one')


if __name__ == '__main__':
    unittest.main()
