"""Six-source compiler in a fresh job directory; no shared cwd or Python installation."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
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


def diagnostic_lines(text: str, limit: int = 8) -> list[str]:
    """Keep primary compiler diagnostics ahead of cascading make/TBE failures.

    The complete log remains the authority. This bounded view is feedback for
    the next candidate edit, where the final make exit is usually unhelpful.
    """
    lines = text.splitlines()
    primary = []
    for index, line in enumerate(lines):
        # A C++ stack frame such as c10::Error::Error is not a compiler error.
        if not re.search(r"(?:fatal )?error:(?!:)|static assertion failed", line, re.I):
            continue
        if line.rstrip().endswith(('named', 'call to')) and index + 1 < len(lines):
            line += ' ' + lines[index + 1].strip()
        primary.append(line)
    fallback = [line for line in lines if re.search(r"error|failed|undefined|not declared", line, re.I)]
    return list(dict.fromkeys(primary or fallback or lines))[:limit]


def _run(argv: list[str], cwd: Path) -> None:
    logger.info("Native build step: %s", argv[0])
    # Keep full diagnostics even if the outer deadline kills the compiler.
    with tempfile.NamedTemporaryFile(prefix=".kernelbridge-build-", suffix=".log", dir=cwd,
                                     delete=False) as output:
        log_path = Path(output.name)
        output.write((json.dumps({"argv": argv}) + "\n").encode())
        output.flush()
        result = subprocess.run(argv, cwd=cwd, check=False, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        with log_path.open("rb") as stream:
            stream.seek(max(0, log_path.stat().st_size - 65536))
            tail = stream.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Exit Code: {result.returncode}\nBuild log: {log_path}\n{tail}")


def _normalize_addconfig(source: str, soc: str) -> str:
    family = next((name for pattern, name in _SOC_FAMILIES if re.search(pattern, soc)), None)
    return re.sub(r'AddConfig\("ascend910[^" ]*"\)', f'AddConfig("{family}")', source) if family else source


def _inject_kernel_include_paths(target: Path, paths) -> None:
    cmake = target / "op_kernel/CMakeLists.txt"
    if not paths or not cmake.is_file():
        return
    text = cmake.read_text()
    modern = "npu_op_kernel_library(" in text
    command = "npu_op_kernel_options(ascendc_kernels ALL OPTIONS" if modern else "add_ops_compile_options(ALL OPTIONS"
    additions = [f'{command} "-I{p}")' for p in paths if p]
    additions = list(dict.fromkeys(line for line in additions if line not in text))
    if not additions:
        return
    extra = "\n".join(additions)
    marker = "npu_op_kernel_library(" if modern else "add_kernels_compile()"
    text = text.replace(marker, extra + "\n" + marker, 1) if marker in text else text.rstrip() + "\n" + extra + "\n"
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
    tiling_headers = [project / directory / (name + "_tiling.h") for directory in ("op_host", "op_kernel")]
    tiling_headers = [path for path in tiling_headers if path.is_file()]
    if len(tiling_headers) != 1:
        raise ValueError("expected one generated tiling header in op_host or op_kernel")
    outputs = {
        tiling_headers[0]: context["host_tiling_src"],
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
    _run([str(installers[0]), "--quiet", "--install-path=" + str(root / "opp")], installers[0].parent)
    _run([sys.executable, "setup.py", "build_ext", "--inplace"], root / "CppExtension")
    source_digest = hashlib.sha256(json.dumps(
        {field: sources[field] for field in _FIELDS}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return capture_outputs(root, source_digest, settings.soc)
