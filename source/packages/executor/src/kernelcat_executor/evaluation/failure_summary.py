"""Bounded observations of an already failed tensor comparison.

This is diagnostic metadata, never a correctness predicate. Callers keep their
original comparator and pass only outputs they have already obtained; this
module never transfers a tensor to a device or invokes a candidate.
"""
import math


_CHUNK_ELEMENTS = 65536
_MAX_EXAMPLES = 4


def _scalar(value):
    if isinstance(value, complex):
        return {"real": _scalar(value.real), "imag": _scalar(value.imag)}
    if isinstance(value, float) and not math.isfinite(value):
        return "nan" if math.isnan(value) else "+inf" if value > 0 else "-inf"
    return value


def summarize_tensor_failure(actual, expected, *, comparison, binding=None,
                             atol=0, rtol=0, require_finite=False):
    """Describe a failure without changing it, including if diagnosis fails.

    ``equal`` follows element equality (NaN differs); ``allclose`` uses the
    caller's original isclose tolerances and optional finite-output requirement.
    ``contract`` reports metadata only. Shapes are never broadcast here.
    """
    report = {"schema_version": "kernelbridge.tensor-failure/v1",
              "comparison": {"kind": comparison, "atol": atol, "rtol": rtol,
                             "require_finite": require_finite},
              "binding": {}, "mismatch_count": None,
              "examples": [], "diagnostic_status": "unavailable"}
    try:
        report["binding"] = dict(binding or {})
        import torch

        def metadata(value):
            if not isinstance(value, torch.Tensor):
                return {"type": type(value).__name__}
            return {"shape": list(value.shape), "dtype": str(value.dtype),
                    "device": str(value.device)}

        report.update(actual=metadata(actual), expected=metadata(expected))
        if not isinstance(actual, torch.Tensor) or not isinstance(expected, torch.Tensor):
            report.update(diagnostic_status="metadata_only", reason="tensor_type_mismatch")
            return report
        if actual.shape != expected.shape:
            report.update(diagnostic_status="metadata_only", reason="shape_mismatch")
            return report
        if comparison == "contract":
            report.update(diagnostic_status="metadata_only", reason="output_contract_failed")
            return report
        if actual.device.type != "cpu" or expected.device.type != "cpu":
            report["reason"] = "cpu_results_unavailable; no device transfer attempted"
            return report
        if comparison not in {"equal", "allclose"}:
            raise ValueError("Unsupported diagnostic comparison")
        # Flattening remains on CPU. The count is complete, but index storage
        # stays bounded even when every element differs in a large output.
        observed, reference = actual.reshape(-1), expected.reshape(-1)
        report["mismatch_count"] = 0
        for offset in range(0, observed.numel(), _CHUNK_ELEMENTS):
            left, right = observed[offset:offset + _CHUNK_ELEMENTS], reference[offset:offset + _CHUNK_ELEMENTS]
            same = (torch.eq(left, right) if comparison == "equal"
                    else torch.isclose(left, right, atol=atol, rtol=rtol, equal_nan=False))
            mismatch = ~same
            if require_finite:
                mismatch = mismatch | ~torch.isfinite(left)
            count = int(torch.count_nonzero(mismatch).item())
            report["mismatch_count"] += count
            remaining = _MAX_EXAMPLES - len(report["examples"])
            if count and remaining:
                indices = torch.nonzero(mismatch, as_tuple=False).flatten()[:remaining].tolist()
                report["examples"].extend({"flat_index": offset + index,
                                           "actual": _scalar(left[index].item()),
                                           "expected": _scalar(right[index].item())}
                                          for index in indices)
        report["diagnostic_status"] = "completed"
    except Exception as error:
        # A partial count cannot be mistaken for a complete comparison.
        report.update(diagnostic_status="unavailable", mismatch_count=None,
                      diagnostic_error={"type": type(error).__name__, "message": str(error)[:512]})
    return report
