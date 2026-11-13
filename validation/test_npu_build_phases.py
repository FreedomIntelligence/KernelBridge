"""Offline driver failure accounting; simulated builds/tensors are not NPU evidence."""
from contextlib import ExitStack, nullcontext, redirect_stdout
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/scatter'))
sys.path.insert(0, str(ROOT / 'source/packages/executor/src'))
DRIVERS = tuple(importlib.import_module(name) for name in ('npu_benchmark', 'npu_matmul_benchmark'))
from kernelcat_executor.backends.ascend import compiler, runtime
from kernelcat_executor.build.outputs import BuildOutputs, BuiltFile


class TensorFixture:
    device = 'npu:1'

    def __init__(self, kind):
        self.kind = kind

    def cpu(self):
        return 'fixture oracle'


class ModelFixture:
    def eval(self):
        return self

    def to(self, device):
        return self

    def __call__(self, *args):
        return TensorFixture('candidate')


class NpuBuildPhaseTests(unittest.TestCase):
    def exercise(self, driver, stage, *, cleanup_error=False, ir_mode=None, profile=False):
        temporary = tempfile.TemporaryDirectory(prefix='kb-driver-phase-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        candidate = root / 'candidate.py'
        # Source parsing remains real; compilation/auditing are explicit fixtures.
        candidate.write_text(''.join(f'{name} = "protocol fixture, never executed"\n'
                                     for name in sorted(driver.FIELDS)))
        is_cube = driver.__name__ == 'npu_matmul_benchmark'
        args = SimpleNamespace(candidate=candidate, output=root / 'job', mode='matmul' if is_cube else 'max',
                               soc='ai_core-Ascend910_9382', device=1, warmup=3,
                               repeats=20 if is_cube else 10, pairs=12 if is_cube else 3, profile=profile)
        instance = SimpleNamespace(device='npu:1', context={'ModelNew': ModelFixture},
                                   load=Mock(), close=Mock())
        if stage == 'load': instance.load.side_effect = RuntimeError('original load failure')
        if cleanup_error: instance.close.side_effect = RuntimeError('secondary cleanup failure')
        factory = Mock(return_value=instance)
        built = None

        def compile_fixture(parsed, settings, **options):
            nonlocal built
            if stage == 'compile': raise RuntimeError('original compile failure')
            artifact = settings.workspace / 'fixture.so'
            artifact.write_bytes(b'simulated build output; not an executable')
            built = BuildOutputs(settings.workspace, 'fixture-source-digest', settings.soc,
                                 (BuiltFile(artifact, hashlib.sha256(artifact.read_bytes()).hexdigest()),))
            return built

        torch = ModuleType('torch')
        torch.__version__ = 'fixture-only'
        torch.no_grad = nullcontext
        torch.equal = lambda left, right: left == right
        torch.npu = SimpleNamespace(synchronize=Mock(), get_device_name=lambda _: 'fixture NPU')
        torch_npu = ModuleType('torch_npu')
        torch_npu.__version__ = 'fixture-only'

        def profiler_fixture(**kwargs):
            csv = args.output / 'profile/partial/kernel_details.csv'
            csv.parent.mkdir(parents=True)
            csv.write_text('fixture only,not a completed trace\n')
            raise RuntimeError('original profiler failure')

        torch_npu.profiler = SimpleNamespace(
            profile=profiler_fixture, ProfilerActivity=SimpleNamespace(CPU='CPU', NPU='NPU'),
            schedule=lambda **kwargs: None, tensorboard_trace_handler=lambda path: path)
        cases = [{'id': f'fixture-{index}', 'seed': index, 'input_sha256': str(index),
                  'src': None, 'index': None, 'a': None, 'b': None, 'expected': 'fixture oracle'}
                 for index in range(3)]

        def measure_fixture(fn, *args, **kwargs):
            if stage == 'measurement': raise RuntimeError('original measurement failure')
            duration = 20 if fn().kind == 'eager' else 10
            return {'us': duration, 'samples_us': [duration]} if is_cube else duration

        def collect_fixture(job):
            if ir_mode is None: return []
            folder = job / 'ir/nested'
            folder.mkdir(parents=True)
            (folder / 'compiler.log').write_text('simulated partial compiler failure\n')
            (folder / 'partial.ll').write_text('invalid partial LLVM fixture\n')
            accepted = folder / 'accepted.ll'
            accepted.write_text('already-validated protocol fixture, not real IR\n')
            rows = [{'status': 'completed'}, {'status': 'unavailable'}]
            if ir_mode == 'failed': rows = [{'status': 'unavailable'}]
            (job / 'ir/receipt.json').write_text(json.dumps(rows))
            if ir_mode == 'interrupted': raise RuntimeError('original collector failure')
            return rows

        def validated_fixture(job, records):
            # The collector has separate provenance tests. This test exercises
            # benchmark consumption of its trusted result, not LLVM validation.
            if ir_mode is None or ir_mode == 'failed': return {}
            self.assertEqual(records, [{'status': 'completed'}, {'status': 'unavailable'}])
            path = job / 'ir/nested/accepted.ll'
            return {str(path.relative_to(job)): hashlib.sha256(path.read_bytes()).hexdigest()}

        check_calls = 0

        def check_fixture(*args, on_failure=None):
            nonlocal check_calls
            check_calls += 1
            if stage == 'recheck' and check_calls == 3:
                raise RuntimeError('original repeated correctness failure')
            return [{'correct': True}]

        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {'torch': torch, 'torch_npu': torch_npu}))
            compilation = stack.enter_context(patch.object(compiler, 'compile_sources', side_effect=compile_fixture))
            stack.enter_context(patch.object(runtime, 'AscendRuntime', factory))
            stack.enter_context(patch.object(driver, 'collect_ir', side_effect=collect_fixture))
            validation = stack.enter_context(patch.object(driver, 'validated_ir_artifacts', side_effect=validated_fixture))
            suite = stack.enter_context(patch.object(driver, 'suite', return_value=cases))
            if stage == 'input_preparation': suite.side_effect = RuntimeError('original input failure')
            stack.enter_context(patch.object(driver, 'measure', side_effect=measure_fixture))
            if is_cube:
                stack.enter_context(patch.object(driver, 'audit_sources', return_value={'fixture': True}))
                stack.enter_context(patch.object(driver, 'check', side_effect=check_fixture))
                stack.enter_context(patch.object(driver, 'matmul_eager', return_value=TensorFixture('eager')))
            else:
                stack.enter_context(patch.object(driver, 'audit_wrapper', return_value={'fixture': True}))
                stack.enter_context(patch.object(driver, 'audit_native_binding', return_value={'fixture': True}))
                stack.enter_context(patch.object(driver, 'scatter_eager', return_value=TensorFixture('eager')))
            with redirect_stdout(io.StringIO()):
                success = driver.run(args)
        result = json.loads((args.output / 'result.json').read_text())
        status = json.loads((args.output / 'status.json').read_text())
        self.assertEqual(result['build_completed'], status['build_completed'])
        self.assertEqual(result['phase'], status['phase'])
        self.assertEqual(success, result['status'] == 'completed')
        return SimpleNamespace(result=result, job=args.output, runtime=instance, factory=factory,
                               compilation=compilation, built=built, validation=validation)

    def assert_built(self, run):
        self.assertTrue(run.result['build_completed'])
        self.assertEqual(run.result['build_source_digest'], run.built.source_digest)
        self.assertEqual(run.result['build_target'], run.built.target)
        self.assertEqual(run.result['build_outputs'], [
            {'path': str(item.path), 'sha256': item.sha256} for item in run.built.files])

    def test_compile_failure_is_not_labeled_as_a_completed_build(self):
        for driver in DRIVERS:
            with self.subTest(driver=driver.__name__):
                run = self.exercise(driver, 'compile')
                self.assertFalse(run.result['build_completed'])
                self.assertFalse(run.result['correct'])
                self.assertEqual(run.result['failure_stage'], 'compile')
                self.assertNotIn('build_outputs', run.result)
                run.factory.assert_not_called()
                run.runtime.close.assert_not_called()

    def test_load_failure_retains_build_and_original_error_when_cleanup_also_fails(self):
        for driver in DRIVERS:
            with self.subTest(driver=driver.__name__):
                run = self.exercise(driver, 'load', cleanup_error=True)
                self.assert_built(run)
                self.assertEqual(run.result['failure_stage'], 'load')
                self.assertEqual(run.result['message'], 'original load failure')
                self.assertEqual(run.result['cleanup_error'], 'secondary cleanup failure')
                self.assertFalse(run.result['correct'])
                run.runtime.close.assert_called_once_with()

    def test_input_failure_does_not_erase_completed_build_identity(self):
        for driver in DRIVERS:
            with self.subTest(driver=driver.__name__):
                run = self.exercise(driver, 'input_preparation')
                self.assert_built(run)
                self.assertEqual(run.result['failure_stage'], 'input_preparation')
                self.assertEqual(run.result['message'], 'original input failure')
                run.runtime.close.assert_called_once_with()

    def test_measurement_failure_keeps_build_true_but_never_qualifies(self):
        for driver in DRIVERS:
            with self.subTest(driver=driver.__name__):
                run = self.exercise(driver, 'measurement')
                self.assert_built(run)
                self.assertEqual(run.result['status'], 'failed')
                self.assertFalse(run.result['correct'])
                expected = 'noise_measurement' if driver.__name__ == 'npu_benchmark' else 'measurement'
                self.assertEqual(run.result['failure_stage'], expected)
                self.assertEqual(run.result['message'], 'original measurement failure')
                self.assertNotIn('geomean_speedup', run.result)
                run.runtime.close.assert_called_once_with()

    def test_repeated_cube_check_is_classified_as_correctness_not_measurement(self):
        run = self.exercise(DRIVERS[1], 'recheck')
        self.assert_built(run)
        self.assertEqual(run.result['failure_stage'], 'correctness')
        self.assertEqual(run.result['message'], 'original repeated correctness failure')
        self.assertIn('F', run.result)
        self.assertFalse(run.result['correct'])

    def test_successful_measurement_followed_by_cleanup_failure_cannot_complete(self):
        for driver in DRIVERS:
            with self.subTest(driver=driver.__name__):
                run = self.exercise(driver, 'success', cleanup_error=True)
                self.assert_built(run)
                self.assertEqual(run.result['status'], 'failed')
                self.assertEqual(run.result['failure_stage'], 'cleanup')
                self.assertFalse(run.result['correct'])
                self.assertTrue(run.result['cases'])
                run.runtime.close.assert_called_once_with()

    def test_partial_ir_retains_valid_files_without_claiming_completion(self):
        for driver in DRIVERS:
            for mode in ('partial', 'interrupted'):
                with self.subTest(driver=driver.__name__, mode=mode):
                    run = self.exercise(driver, 'load', ir_mode=mode)
                    report = next(row for row in run.result['diagnostics']['reports'] if row['kind'] == 'ir')
                    self.assertEqual(report['status'], 'failed')
                    self.assertTrue(report['partial'])
                    self.assertEqual(set(report['artifacts']), {'ir/nested/accepted.ll'})
                    self.assertEqual(set(report['attempt_evidence']), {'ir/receipt.json', 'ir/nested/compiler.log'})
                    self.assertFalse(run.result['diagnostics']['satisfied'])
                    run.validation.assert_called_once()
                    if mode == 'interrupted': self.assertIn('original collector failure', report['reason'])

    def test_failed_recipe_partial_files_are_never_accepted_by_suffix(self):
        for driver in DRIVERS:
            with self.subTest(driver=driver.__name__):
                run = self.exercise(driver, 'load', ir_mode='failed')
                report = next(row for row in run.result['diagnostics']['reports'] if row['kind'] == 'ir')
                self.assertTrue((run.job / 'ir/nested/partial.ll').exists())
                self.assertEqual(report['status'], 'unavailable')
                self.assertEqual(report['artifacts'], {})
                self.assertFalse(run.result['diagnostics']['satisfied'])

    def test_profiler_exception_with_leftover_csv_is_partial_not_complete(self):
        for driver in DRIVERS:
            with self.subTest(driver=driver.__name__):
                run = self.exercise(driver, 'success', profile=True)
                self.assertEqual(run.result['status'], 'completed')
                report = next(row for row in run.result['diagnostics']['reports'] if row['kind'] == 'profiler')
                self.assertEqual(report['status'], 'failed')
                self.assertTrue(report['partial'])
                self.assertEqual(report['reason'], 'original profiler failure')
                self.assertEqual(set(report['artifacts']), {'profile/partial/kernel_details.csv'})
                self.assertFalse(run.result['diagnostics']['satisfied'])


if __name__ == '__main__':
    unittest.main()
