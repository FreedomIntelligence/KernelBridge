"""Six-source compiler in a fresh job directory; no shared cwd or Python installation."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from kernelcat_executor.build.inputs import BuildSettings
from kernelcat_executor.build.outputs import BuildOutputs, capture_outputs

logger = logging.getLogger(__name__)
_SOC_FAMILIES = ((r"Ascend910_93", "ascend910_93"), (r"Ascend910_95", "ascend910_95"),
                 (r"Ascend910_55", "ascend910_55"), (r"Ascend910_96", "ascend910_96"),
                 (r"Ascend910B", "ascend910b"), (r"Ascend910A", "ascend910"),
                 (r"Ascend310P", "ascend310p"), (r"Ascend310B", "ascend310b"))
_FIELDS = ("project_json_src", "host_tiling_src", "host_operator_src",
           "kernel_src", "python_bind_src")


def _run(argv: list[str], cwd: Path) -> None:
    logger.info("Native build step: %s", argv[0])
    result = subprocess.run(argv, cwd=cwd, check=False, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"Exit Code: {result.returncode}\n[STDOUT]\n{result.stdout}\n[STDERR]\n{result.stderr}")


def _normalize_addconfig(source: str, soc: str) -> str:
    family = next((name for pattern, name in _SOC_FAMILIES if re.search(pattern, soc)), None)
    return re.sub(r'AddConfig\("ascend910[^" ]*"\)', f'AddConfig("{family}")', source) if family else source


def _inject_kernel_include_paths(target: Path, paths) -> None:
    cmake = target / "op_kernel/CMakeLists.txt"
    if not paths or not cmake.is_file():
        return
    text = cmake.read_text()
    additions = [f"add_ops_compile_options(ALL OPTIONS -I{p})" for p in paths if p]
    additions = list(dict.fromkeys(line for line in additions if line not in text))
    if not additions:
        return
    extra = "\n".join(additions)
    text = text.replace("add_kernels_compile()", extra + "\nadd_kernels_compile()", 1) if "add_kernels_compile()" in text else text.rstrip() + "\n" + extra + "\n"
    cmake.write_text(text)


def compile_sources(sources: Mapping[str, str], settings: BuildSettings,
                    extra_kernel_include_paths: tuple[str, ...] = ()) -> BuildOutputs:
    """Compile already parsed sources in the explicitly provisioned native process."""
    if any(not sources.get(field, "").strip() for field in _FIELDS):
        raise ValueError("compiler requires five nonempty native source strings")
    context = sources
    op = settings.operator
    root = settings.workspace
    name = op + "_custom"
    project = root / "".join(part.capitalize() for part in name.split("_") if part)
    if project.exists():
        raise FileExistsError("native project already exists; reconcile the original attempt")
    (root / (name + ".json")).write_text(context["project_json_src"])
    _run(["msopgen", "gen", "-i", name + ".json", "-c", settings.soc,
          "-lan", "cpp", "-out", project.name], root)
    outputs = {
        project / "op_host" / (name + "_tiling.h"): context["host_tiling_src"],
        project / "op_host" / (name + ".cpp"): _normalize_addconfig(context["host_operator_src"], settings.soc),
        project / "op_kernel" / (name + ".cpp"): context["kernel_src"],
        root / "CppExtension/csrc/op.cpp": context["python_bind_src"],
    }
    for path, text in outputs.items():
        path.write_text(text)
    _inject_kernel_include_paths(project, extra_kernel_include_paths)
    # Keep the established native build/deploy protocol; isolate it per job.
    os.environ.pop("ASCEND_CUSTOM_OPP_PATH", None)
    _run(["./build.sh"], project)
    installers = sorted((project / "build_out").glob("custom_opp_*.run"))
    if len(installers) != 1:
        raise ValueError("expected one unambiguous native installer")
    _run([str(installers[0])], installers[0].parent)
    _run([sys.executable, "setup.py", "build_ext", "--inplace"], root / "CppExtension")
    source_digest = hashlib.sha256(json.dumps(
        {field: sources[field] for field in _FIELDS}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return capture_outputs(root, source_digest, settings.soc)
