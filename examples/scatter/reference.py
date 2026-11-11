"""Independent finite FP32 feature-index oracle; no torch or native candidate imports.

Contract: src [rows, features], index [rows], output [dim_size, features].
Unreferenced bins are zero; values only, no argmax/argmin output. Inputs are
strictly inside FP32 extrema, avoiding torch_scatter's sentinel ambiguity.
"""
import math


def scatter(src, index, dim_size, reduce):
    if reduce not in ('max', 'min') or type(dim_size) is not int or dim_size < 0:
        raise ValueError('reduce must be max/min and dim_size nonnegative')
    if len(src) != len(index): raise ValueError('one feature index per source row')
    features = len(src[0]) if src else 0
    if any(len(row) != features for row in src): raise ValueError('ragged features')
    if any(type(i) is not int or i < 0 or i >= dim_size for i in index): raise ValueError('index out of bounds')
    if any(not math.isfinite(x) or abs(x) >= 3.4028234663852886e38 for row in src for x in row):
        raise ValueError('contract requires finite FP32 values strictly inside extrema')
    reduce_fn = max if reduce == 'max' else min
    # Group-and-reduce oracle intentionally differs from the candidate's scatter.
    groups = [[row for row, destination in zip(src, index) if destination == bin_] for bin_ in range(dim_size)]
    return [[reduce_fn(row[col] for row in group) if group else 0.0 for col in range(features)] for group in groups]


def scatter_max(src, index, dim_size): return scatter(src, index, dim_size, 'max')
def scatter_min(src, index, dim_size): return scatter(src, index, dim_size, 'min')
