"""Compile, check, then measure; the native device is owned by one evaluation."""
from kernelcat_executor.evaluation.measurement.statistics import summarize_event_samples_ms
from kernelcat_executor.evaluation.protocols.six_source.source import extract_first_code


def evaluate(candidate: str, reference: str, backend, *, fast: bool = False) -> dict:
    result = {"compiled": False, "correctness": False, "performance": None,
              "hardware": backend.settings.soc, "failure_stage": None}
    if hasattr(backend, "tolerances"):
        result["correctness_policy"] = dict(backend.tolerances)
    try:
        compiled, info = backend.compile(extract_first_code(candidate))
        if getattr(backend, "build_manifest", None) is not None:
            result["build"] = backend.build_manifest
        if not compiled:
            result.update(compile_info=info, failure_stage=getattr(backend, "failure_stage", "compile_error"))
            return result
        result["compiled"] = True
        correct, info, stage = backend.correctness(reference)
        if not correct:
            result.update(correctness_info=info, failure_stage=stage)
            return result
        result["correctness"] = True
        if not fast:
            result["performance"] = summarize_event_samples_ms(backend.measure())
        return result
    finally:
        # Cleanup is required on compile failure, correctness failure and exceptions too.
        backend.close()
