"""Six-source protocol composes native build/load with its own correctness/timing."""
from __future__ import annotations

from kernelcat_executor.backends.ascend.compiler import compile_sources
from kernelcat_executor.backends.ascend.runtime import AscendRuntime
from kernelcat_executor.backends.ports import Compiler
from kernelcat_executor.build.inputs import BuildSettings
from kernelcat_executor.evaluation.measurement.ascend_events import time_execution_event_template
from kernelcat_executor.evaluation.protocols.six_source.correctness import execute_template
from kernelcat_executor.evaluation.protocols.six_source.config import NUM_PERF_TRIALS, NUM_WARMUP

_FIELDS = ("project_json_src", "host_tiling_src", "host_operator_src",
           "kernel_src", "python_bind_src", "model_src")


class AscendBackend:
    def __init__(self, settings: BuildSettings, *, compiler: Compiler = compile_sources, atol=1e-4, rtol=1e-4):
        self.runtime = AscendRuntime(settings)
        self.compiler = compiler
        self.settings = settings
        self.context = self.runtime.context
        self.failure_stage = "compile_error"
        self.build_manifest = None
        self.tolerances = {"atol": atol, "rtol": rtol}

    def compile(self, generated_code: str):
        try:
            exec(compile(generated_code, "<candidate-six-source>", "exec"), self.context)
            if any(not isinstance(self.context.get(field), str) or
                   not self.context[field].strip() for field in _FIELDS):
                raise ValueError("candidate must define all six nonempty source strings")
            outputs = self.compiler({field: self.context[field] for field in _FIELDS}, self.settings)
            self.build_manifest = {
                "source_digest": outputs.source_digest, "target": outputs.target,
                "files": [{"path": str(item.path.relative_to(outputs.workspace)), "sha256": item.sha256}
                          for item in outputs.files],
            }
            self.failure_stage = "load_error"
            self.runtime.load(outputs, self.context["model_src"])
            return True, None
        except Exception as error:
            return False, str(error)

    def correctness(self, reference: str):
        try:
            exec(compile(reference, "<frozen-reference>", "exec"), self.context)
        except Exception as error:
            return False, f"reference setup failed: {error}", "runtime_error"
        return execute_template(self.runtime.torch_npu.npu.synchronize,
                                self.runtime.device, self.context, **self.tolerances)

    def measure(self):
        return time_execution_event_template(self.context, self.runtime.device,
            self.runtime.torch_npu.npu.synchronize, self.runtime.torch_npu.npu.Event, "ModelNew",
            num_perf_trials=NUM_PERF_TRIALS, num_warmup=NUM_WARMUP)

    def close(self):
        self.runtime.close()
