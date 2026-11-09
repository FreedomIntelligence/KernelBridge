"""Loads verified job-local outputs only inside an admitted native process."""
from __future__ import annotations

import os
import sys
from typing import Any

from kernelcat_executor.build.inputs import BuildSettings
from kernelcat_executor.build.outputs import BuildOutputs, verify_outputs


class AscendRuntime:
    def __init__(self, settings: BuildSettings) -> None:
        import torch
        import torch_npu

        self.torch_npu = torch_npu
        self.settings = settings
        self.context: dict[str, Any] = {}
        self.device = torch.device(f"npu:{settings.device}")
        # ASCEND_DEVICE_ID does not select torch_npu's current device.
        torch_npu.npu.set_device(self.device)

    def load(self, outputs: BuildOutputs, model_source: str) -> None:
        verify_outputs(outputs, self.settings.workspace)
        if outputs.target != self.settings.soc:
            raise ValueError("build target differs from native runtime binding")
        customize = self.settings.workspace / "opp/vendors/customize"
        os.environ["ASCEND_CUSTOM_OPP_PATH"] = str(customize)
        local_lib = str(customize / "op_api/lib")
        libraries = os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if local_lib not in libraries:
            os.environ["LD_LIBRARY_PATH"] = os.pathsep.join([local_lib, *libraries])
        # The extension is job-local and this interpreter is a disposable native child.
        sys.path.insert(0, str(self.settings.workspace / "CppExtension"))
        exec(compile(model_source, "<candidate-model>", "exec"), self.context)

    def close(self) -> None:
        self.context.clear()
        self.torch_npu.npu.empty_cache()
        self.torch_npu.npu.synchronize(device=self.device)
