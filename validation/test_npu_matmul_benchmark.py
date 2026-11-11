"""Local interface tests only: no candidate creation, compilation or NPU dispatch."""
import ast
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/scatter'))
sys.path.insert(0, str(ROOT / 'source/packages/executor/src'))
spec = importlib.util.spec_from_file_location('cube_benchmark', ROOT / 'examples/scatter/npu_matmul_benchmark.py')
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class FakeNpu:
    def __init__(self, milliseconds=0.01):
        self.events = 0
        self.synchronizations = 0
        self.records = []
        self.milliseconds = milliseconds

    def device(self, device):
        return nullcontext()

    def current_stream(self, device):
        return ('stream', device)

    def synchronize(self, *, device):
        self.synchronizations += 1

    def Event(self, *, enable_timing):
        if enable_timing is not True:
            raise AssertionError('Timing must be explicitly enabled')
        self.events += 1
        return SimpleNamespace(record=lambda stream: self.records.append(stream),
                               elapsed_time=lambda end: self.milliseconds)


class CubeBenchmarkTests(unittest.TestCase):
    def test_original_case_and_seed_contract_unchanged(self):
        original = json.loads((ROOT / 'examples/cann/cube-matmul/case.json').read_text())
        self.assertEqual(benchmark.CONTRACT['shape'], [original[key] for key in ('m', 'n', 'k')])
        for key in ('dtype', 'accumulator_dtype', 'seeds', 'distribution', 'atol', 'rtol'):
            self.assertEqual(benchmark.CONTRACT[key], original[key])
        self.assertEqual(benchmark.CONTRACT['F'], {'warmup': 3, 'samples': 100, 'timer': 'npu_event', 'timing_seed': 1024})
        self.assertEqual(benchmark.CONTRACT['G']['aggregation'], 'paired_block_log_mean')
        self.assertTrue(benchmark.CONTRACT['G']['require_noise'])

    @unittest.skipUnless(importlib.util.find_spec('torch'), 'CPU Torch is unavailable; device tests remain separate')
    def test_inputs_and_untimed_oracle_equal_original_reference(self):
        import torch
        original_spec = importlib.util.spec_from_file_location('original_cube_reference', ROOT / 'examples/cann/cube-matmul/reference.py')
        original = importlib.util.module_from_spec(original_spec)
        original_spec.loader.exec_module(original)
        cases = benchmark.suite(torch, torch.device('cpu'))
        self.assertEqual(len(cases), 5)
        for case in cases:
            a, b = original.get_inputs()
            self.assertTrue(torch.equal(a, case['a']))
            self.assertTrue(torch.equal(b, case['b']))
            self.assertTrue(torch.equal(original.Model()(a, b), case['oracle']))
            self.assertEqual(case['oracle'].dtype, torch.float16)
        with self.assertRaisesRegex(ValueError, 'measured NPU'):
            benchmark.matmul_eager(cases[0]['a'], cases[0]['b'])

    def test_F_uses_exactly_original_100_event_samples(self):
        npu = FakeNpu()
        calls = []
        measured = benchmark.measure(lambda: calls.append(True), SimpleNamespace(npu=npu), 'npu:1', samples=100)
        self.assertEqual(len(calls), 103)
        self.assertEqual(npu.events, 200)
        self.assertEqual(npu.synchronizations, 103)
        self.assertEqual(npu.records, [('stream', 'npu:1')] * 200)
        self.assertEqual(measured, {'us': 10, 'samples_us': [10] * 100})

    def test_nonpositive_event_does_not_become_a_speedup(self):
        for milliseconds in (0, -1, float('nan'), float('inf')):
            with self.subTest(milliseconds=milliseconds), self.assertRaises(ValueError):
                benchmark.measure(lambda: None, SimpleNamespace(npu=FakeNpu(milliseconds)), 'npu:1', samples=1)

    def test_G_and_independent_null_keep_all_samples_and_seed_rotation(self):
        cases = [{'seed': seed, 'input_sha256': str(seed), 'a': seed, 'b': seed} for seed in benchmark.SEEDS]
        for order in (('eager', 'candidate', 'candidate', 'eager'), ('eager',) * 4):
            with self.subTest(order=order):
                npu = FakeNpu()
                calls = []
                checkpoints = []
                functions = {name: lambda a, b, name=name: calls.append((name, a, b)) for name in set(order)}
                with patch.object(benchmark, 'check', return_value=[{'correct': True}] * 5) as check:
                    arms = benchmark.paired(functions, cases, SimpleNamespace(npu=npu), 'npu:1', order=order,
                                             checkpoint=lambda rows: checkpoints.append(len(rows)))
                self.assertEqual(len(arms), 48)
                self.assertEqual(check.call_count, 48)
                self.assertTrue(all(call.args[1] is cases for call in check.call_args_list))
                self.assertEqual(checkpoints, list(range(1, 49)))
                self.assertEqual(len(calls), 48 * 23)
                self.assertEqual(npu.events, 48 * 20 * 2)
                for index, row in enumerate(arms):
                    self.assertEqual(row['pair'], index // 4)
                    self.assertEqual(row['position'], index % 4)
                    self.assertEqual(row['arm'], order[index % 4])
                    self.assertEqual(row['seed'], 1024 + (index // 4) % 5)
                    self.assertEqual(row['samples_us'], [10] * 20)

    def test_measurement_override_fails_before_creating_job(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'job'
            for warmup, repeats, pairs in ((0, 20, 12), (3, 1, 12), (3, 20, 1)):
                with self.subTest(values=(warmup, repeats, pairs)), self.assertRaises(ValueError):
                    benchmark.run(SimpleNamespace(output=output, warmup=warmup, repeats=repeats, pairs=pairs))
                self.assertFalse(output.exists())

    def test_outer_source_parser_never_executes_imports(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'invalid-source.txt'
            sentinel = Path(temp) / 'executed'
            path.write_text(f'from pathlib import Path\nPath({str(sentinel)!r}).touch()\n')
            with self.assertRaises(ValueError):
                benchmark.sources(path)
            self.assertFalse(sentinel.exists())

    def test_original_stateless_wrapper_accepted_without_executing_candidate(self):
        tree = ast.parse((ROOT / 'examples/cann/cube-matmul/baseline.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == 'model_src')
        source = ast.literal_eval(node.value)
        self.assertEqual(benchmark.audit_wrapper(source), {'six_source_audit': 'passed', 'inherited_module_init': True})
        for invalid in (source.replace('custom_ops_lib.matmul_custom', 'torch.matmul'),
                        source + '\ntorch.set_default_device("cpu")\n',
                        source.replace('def forward(self, a, b):', 'def forward(self, a, b=torch.ones(1)):')):
            with self.subTest(source=invalid), self.assertRaises(ValueError):
                benchmark.audit_wrapper(invalid)

    def test_generic_scatter_binding_uses_audit_copy_only(self):
        source = 'import torch\nimport custom_ops_lib\nclass ModelNew(torch.nn.Module):\n    def forward(self, src, index):\n        return custom_ops_lib.run(src, index)\n'
        before = source
        self.assertEqual(benchmark.audit_wrapper(source, call_name='run')['six_source_audit'], 'passed')
        self.assertEqual(source, before)
        typed = source.replace('def forward(self, src, index):', 'def forward(self, src: torch.Tensor, index: torch.Tensor) -> torch.Tensor:')
        self.assertEqual(benchmark.audit_wrapper(typed, call_name='run')['six_source_audit'], 'passed')
        with self.assertRaises(ValueError):
            benchmark.audit_wrapper(source.replace('src, index):', 'src: torch.ones(1), index):'), call_name='run')
        with self.assertRaises(ValueError):
            benchmark.audit_wrapper(source)
        for native in ('return tensor.cpu();', 'return at::amax(x);', 'return x.to(torch::kCPU);'):
            with self.subTest(native=native), self.assertRaises(ValueError):
                benchmark.audit_native_binding(native)

    def test_existing_native_custom_binding_passes_source_screen(self):
        # Read-only static audit of an existing fixture; it is never a trial input.
        tree = ast.parse((ROOT / 'examples/cann/cube-matmul/baseline.py').read_text())
        parsed = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
                  if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                  and n.targets[0].id in benchmark.FIELDS - {'project_json_src'}}
        self.assertEqual(benchmark.audit_sources(parsed)['native_cube_source_screen'], 'passed')
        for forbidden in ('at::matmul(a,b);', 'aclnnMatmul(a,b);', 'aclnnMatmulGetWorkspaceSize(a,b);'):
            with self.subTest(forbidden=forbidden), self.assertRaises(ValueError):
                benchmark.audit_sources(dict(parsed, python_bind_src=forbidden))

    def test_receipt_file_hash_binds_exact_serialization(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'noise.json'
            payload = {'schema_version': 'kernelbridge.noise-calibration/v1', 'arms': []}
            benchmark.save_json(path, payload)
            self.assertEqual(path.read_text(), json.dumps(payload, indent=2, allow_nan=False) + '\n')
            self.assertEqual(len(benchmark.digest(path)), 64)
            with self.assertRaises(ValueError):
                benchmark.save_json(path, {'us': float('nan')})
            self.assertEqual(json.loads(path.read_text()), payload)


if __name__ == '__main__':
    unittest.main()
