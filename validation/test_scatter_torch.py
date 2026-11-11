"""Differential tests against the actual installed torch_scatter CPU extension."""
import importlib.util
from pathlib import Path
import unittest

import torch
import torch_scatter

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('candidate', ROOT / 'examples/scatter/torch_api.py')
candidate = importlib.util.module_from_spec(spec); spec.loader.exec_module(candidate)


class ScatterTorchTests(unittest.TestCase):
    def test_broadcast_dimension_dtype_empty_and_noncontiguous(self):
        count = 0
        for reduce in ('min', 'max'):
            for dtype in (torch.float16, torch.float32, torch.float64, torch.int32, torch.int64):
                for shape, dim, index_shape, bins in [((7, 5), 0, (7,), 11), ((3, 7, 5), 1, (7,), 4),
                    ((3, 7, 5), -1, (3, 7, 5), 9), ((0, 5), 0, (0,), 3), ((4, 0), -1, (0,), 0)]:
                    g = torch.Generator().manual_seed(17)
                    src = torch.randint(-8, 8, shape, generator=g).to(dtype)
                    index = torch.randint(max(bins, 1), index_shape, generator=g)
                    before = src.clone(); ids = index.clone()
                    actual = candidate.scatter(src, index, dim=dim, dim_size=bins, reduce=reduce)
                    expected = torch_scatter.scatter(src, index, dim=dim, dim_size=bins, reduce=reduce)
                    torch.testing.assert_close(actual, expected, rtol=0, atol=0)
                    self.assertIsInstance(actual, torch.Tensor)
                    self.assertTrue(torch.equal(before, src)); self.assertTrue(torch.equal(ids, index)); count += 1
                src = torch.arange(24).reshape(4, 6).t().to(dtype)
                index = torch.tensor([1, 0, 1, 0, 1, 0])
                torch.testing.assert_close(candidate.scatter(src, index, dim=0, reduce=reduce),
                                           torch_scatter.scatter(src, index, dim=0, reduce=reduce), rtol=0, atol=0)
        self.assertEqual(count, 50)

    def test_negative_values_empty_bins_and_explicit_out(self):
        for reduce in ('max', 'min'):
            src = torch.tensor([[-8., 6.], [-3., 2.], [-3., 4.], [0., -0.]])
            index = torch.tensor([1, 1, 1, 2])
            out = torch.full((5, 2), -2. if reduce == 'max' else 3.)
            expected = torch_scatter.scatter(src, index, dim=0, out=out.clone(), reduce=reduce)
            actual = candidate.scatter(src, index, dim=0, out=out, reduce=reduce)
            self.assertEqual(actual.data_ptr(), out.data_ptr())
            torch.testing.assert_close(actual, expected, rtol=0, atol=0)
            plain = candidate.scatter(src, index, dim=0, dim_size=5, reduce=reduce)
            self.assertTrue(torch.equal(plain[0], torch.zeros(2)))
            self.assertTrue(torch.equal(plain[4], torch.zeros(2)))

    def test_dtype_extrema_match_upstream_sentinel_semantics(self):
        for dtype in (torch.float32, torch.int64):
            info = torch.finfo(dtype) if dtype.is_floating_point else torch.iinfo(dtype)
            for reduce, value in (('max', info.min), ('min', info.max)):
                src = torch.tensor([value], dtype=dtype); index = torch.tensor([0])
                self.assertTrue(torch.equal(candidate.scatter(src, index, reduce=reduce), torch_scatter.scatter(src, index, reduce=reduce)))

    def test_invalid_inputs_fail_before_execution(self):
        for index in (torch.tensor([-1]), torch.tensor([2])):
            with self.assertRaisesRegex(ValueError, 'bounds'): candidate.scatter(torch.tensor([1.]), index, dim_size=2)
        with self.assertRaisesRegex(ValueError, 'finite'): candidate.scatter(torch.tensor([float('nan')]), torch.tensor([0]))
        with self.assertRaisesRegex(ValueError, 'int64'): candidate.scatter(torch.tensor([1.]), torch.tensor([0], dtype=torch.int32))


if __name__ == '__main__':
    print('torch=' + torch.__version__ + ' torch_scatter=' + torch_scatter.__version__)
    unittest.main()
