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
import time
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
_BUILD_DIAGNOSTICS_SCHEMA = "kernelbridge.native-build-diagnostics/v1"
_BUILD_BINDING = ".kernelbridge-build-binding.json"


def _canonical_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _write_receipt(path: Path, payload: dict) -> None:
    """Publish a complete receipt, never a partially written JSON document."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=path.name + ".",
                                         suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"payload": payload, "payload_sha256": _canonical_digest(payload)}, stream,
                      ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _read_receipt(path: Path) -> dict:
    envelope = json.loads(path.read_text())
    payload = envelope["payload"]
    if not isinstance(payload, dict) or envelope.get("payload_sha256") != _canonical_digest(payload):
        raise ValueError("Build receipt payload hash mismatch")
    return payload


def _artifact(path: Path, job: Path, *, workspace: Path | None = None) -> dict:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(job):
        raise ValueError("Build diagnostic artifact escapes the original job")
    # A bound workspace may itself be reached through an alias. Links below
    # that root are not immutable artifacts and must not be silently followed.
    root = (workspace or job).resolve(strict=True)
    lexical = path.absolute()
    prefix = next((parent for parent in lexical.parents if parent.resolve() == root), None)
    if prefix is None:
        raise ValueError("Build diagnostic artifact is outside the bound workspace")
    current = prefix
    for component in lexical.relative_to(prefix).parts:
        current /= component
        if current.is_symlink():
            raise ValueError("Build diagnostic artifact contains an internal symbolic link")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return {"path": resolved.relative_to(job).as_posix(), "sha256": digest.hexdigest(), "bytes": size}


def _first_sdk_diagnostic(path: Path, relative: str) -> dict | None:
    with path.open(errors="replace") as stream:
        for number, line in enumerate(stream, 1):
            # A final result JSON can quote earlier errors; it is not a new diagnostic.
            if line.lstrip().startswith(("{", "[")):
                try:
                    if isinstance(json.loads(line), (dict, list)):
                        continue
                except ValueError:
                    pass
            if re.search(r"(?:^\s*\[E\d{4}(?:\s|\])|\[ERROR\]|\bExecution_Error(?:\(|:))", line):
                return {"text": line.rstrip()[:2048], "line": number, "path": relative}
    return None


def collect_build_diagnostics(workspace: Path) -> dict:
    """Collect verified original build receipts; status describes collection, not build success."""
    root = Path(workspace).resolve()
    job = root.parent
    result = {"schema_version": _BUILD_DIAGNOSTICS_SCHEMA, "status": "unavailable",
              "source_digest": None, "target": None, "steps": [], "artifacts": {}, "issues": []}
    binding_path = root / _BUILD_BINDING
    if not binding_path.is_file():
        result["reason"] = "Original build has no source-bound step receipts"
        return result
    try:
        if not binding_path.resolve(strict=True).is_relative_to(root):
            raise ValueError("Build binding escapes its original workspace")
        descriptor = _artifact(binding_path, job, workspace=root)
        binding = _read_receipt(binding_path)
        if (binding.get("workspace") != str(root) or binding.get("job") != str(job)
                or not re.fullmatch(r"[0-9a-f]{64}", str(binding.get("source_digest", "")))
                or not isinstance(binding.get("target"), str) or not binding["target"]):
            raise ValueError("Build receipt source/target/workspace binding is invalid")
        result.update(source_digest=binding["source_digest"], target=binding["target"])
        result["artifacts"][descriptor["path"]] = descriptor["sha256"]
        logs = set(root.rglob(".kernelbridge-build-*.log"))
        # A missing log must not make its surviving receipt silently disappear.
        logs.update(Path(str(path).removesuffix(".receipt.json") + ".log")
                    for path in root.rglob(".kernelbridge-build-*.receipt.json"))
        for log in sorted(logs):
            metadata_path = log.with_suffix(".receipt.json")
            try:
                if any(not path.resolve(strict=True).is_relative_to(root) for path in (log, metadata_path)):
                    raise ValueError("Build step artifact escapes its original workspace")
                metadata_record = _artifact(metadata_path, job, workspace=root)
                metadata = _read_receipt(metadata_path)
                if metadata.get("binding") != binding:
                    raise ValueError("Build step source/target/workspace binding changed")
                cwd = Path(metadata["cwd"]).resolve(strict=True)
                if (not cwd.is_relative_to(root) or not isinstance(metadata.get("argv"), list)
                        or not metadata["argv"] or any(not isinstance(arg, str) for arg in metadata["argv"])):
                    raise ValueError("Build step command is outside its original workspace")
                if type(metadata.get("started_at_ns")) is not int:
                    raise ValueError("Build step lacks its original start time")
                returncode = metadata["returncode"]
                if returncode is not None and type(returncode) is not int:
                    raise ValueError("Build step has no real process return code")
                log_record = _artifact(log, job, workspace=root)
                if metadata.get("log") != log_record:
                    raise ValueError("Build step log hash/size/path mismatch")
                step = {"argv": metadata["argv"], "cwd": metadata["cwd"], "returncode": returncode,
                        "started_at_ns": str(metadata["started_at_ns"]), "log": log_record, "metadata": metadata_record}
                diagnostic = _first_sdk_diagnostic(log, log_record["path"])
                if diagnostic is not None:
                    step["first_sdk_diagnostic"] = diagnostic
                if returncode is None:
                    step["error"] = metadata.get("error", "Build process did not return a completion code")
                result["steps"].append(step)
                for record in (log_record, metadata_record):
                    result["artifacts"][record["path"]] = record["sha256"]
            except (OSError, ValueError, KeyError, TypeError) as error:
                result["issues"].append({"path": str(log.relative_to(job)), "reason": str(error)})
        result["steps"].sort(key=lambda step: int(step["started_at_ns"]))
        if result["steps"]:
            result["status"] = "partial" if result["issues"] or any(
                step["returncode"] is None for step in result["steps"]) else "completed"
        else:
            result["reason"] = "Original build produced no verified completed step receipt"
    except (OSError, ValueError, KeyError, TypeError) as error:
        result["reason"] = "Build diagnostic collection failed: " + str(error)
    return result


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


def _project_include_roots(project: Path) -> tuple[Path, ...]:
    """One ordered, job-local include contract shared by all generated phases."""
    root = project.resolve(strict=True)
    paths = tuple((root / relative).resolve(strict=True) for relative in (".", "op_host", "op_kernel"))
    if any(not path.is_dir() or not path.is_relative_to(root) for path in paths):
        raise ValueError("generated include roots must be directories inside the native project")
    # CPATH uses a path separator, and the existing kernel CMake API uses quoted
    # arguments. Reject ambiguous encodings instead of silently changing paths.
    if any(any(character in str(path) for character in (os.pathsep, "\n", "\r", '"', "$", ";", "\\")) for path in paths):
        raise ValueError("generated include root cannot be encoded unambiguously")
    return paths


def _run(argv: list[str], cwd: Path, *, include_roots: tuple[Path, ...] = (),
         build_binding: dict | None = None) -> None:
    logger.info("Native build step: %s", argv[0])
    environment = os.environ.copy()
    inherited = tuple(path for path in environment.get("CPATH", "").split(os.pathsep) if path)
    if include_roots:
        # Preserve the activated profile's SDK/Python headers after job roots.
        # Empty CPATH entries imply cwd; never add that implicit search root.
        environment["CPATH"] = os.pathsep.join((*map(str, include_roots), *inherited))
        environment.pop("ASCEND_CUSTOM_OPP_PATH", None)
    # Keep full diagnostics even if the outer deadline kills the compiler.
    started_at_ns = time.time_ns()
    result = None
    log_path = None
    process_error = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".kernelbridge-build-", suffix=".log", dir=cwd,
                                         delete=False) as output:
            log_path = Path(output.name)
            output.write((json.dumps({"argv": argv, "cwd": str(cwd),
                "include_contract": "kernelbridge.generated-includes/v1" if include_roots else None,
                "include_roots": list(map(str, include_roots)),
                "inherited_include_count": len(inherited) if include_roots else None,
                "inherited_include_sha256": hashlib.sha256(json.dumps(inherited,
                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() if include_roots else None,
                "include_transport": "child_env_CPATH" if include_roots else None}) + "\n").encode())
            output.flush()
            result = subprocess.run(argv, cwd=cwd, env=environment, check=False, stdout=output, stderr=subprocess.STDOUT)
    except BaseException as error:
        process_error = type(error).__name__ + ": " + str(error)
        raise
    finally:
        if log_path is not None:
            try:
                job = Path(build_binding["job"]) if build_binding else Path(cwd).resolve().parent
                workspace = Path(build_binding["workspace"]) if build_binding else Path(cwd).resolve()
                receipt = {"binding": build_binding, "argv": argv, "cwd": str(Path(cwd).resolve()),
                           "started_at_ns": started_at_ns, "ended_at_ns": time.time_ns(),
                           "returncode": result.returncode if result is not None else None,
                           "log": _artifact(log_path, job, workspace=workspace)}
                if process_error is not None:
                    receipt["error"] = process_error
                _write_receipt(log_path.with_suffix(".receipt.json"), receipt)
            except Exception as error:
                # Missing metadata is reported by the collector, never as a replacement process failure.
                logger.warning("Unable to retain native build receipt for %s: %s", log_path, error)
    if result.returncode:
        with log_path.open("rb") as stream:
            stream.seek(max(0, log_path.stat().st_size - 65536))
            tail = stream.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Exit Code: {result.returncode}\nBuild log: {log_path}\n{tail}")


def _normalize_addconfig(source: str, soc: str) -> str:
    family = next((name for pattern, name in _SOC_FAMILIES if re.search(pattern, soc)), None)
    return re.sub(r'AddConfig\("ascend910[^" ]*"\)', f'AddConfig("{family}")', source) if family else source


def _inject_kernel_include_paths(target: Path, paths, options=()) -> None:
    cmake = target / "op_kernel/CMakeLists.txt"
    if not (paths or options) or not cmake.is_file():
        return
    text = cmake.read_text()
    modern = "npu_op_kernel_library(" in text
    command = "npu_op_kernel_options(ascendc_kernels ALL OPTIONS" if modern else "add_ops_compile_options(ALL OPTIONS"
    additions = [f'{command} "-I{p}")' for p in paths if p]
    additions.extend(f'{command} "{option}")' for option in options)
    additions = list(dict.fromkeys(line for line in additions if line not in text))
    if not additions:
        return
    extra = "\n".join(additions)
    marker = "npu_op_kernel_library(" if modern else "add_kernels_compile()"
    text = text.replace(marker, extra + "\n" + marker, 1) if marker in text else text.rstrip() + "\n" + extra + "\n"
    cmake.write_text(text)


def compile_sources(sources: Mapping[str, str], settings: BuildSettings,
                    extra_kernel_include_paths: tuple[str, ...] = (), *,
                    diagnostic_options: tuple[str, ...] = ()) -> BuildOutputs:
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
    source_digest = _canonical_digest({field: sources[field] for field in _FIELDS})
    binding = {"workspace": str(root.resolve()), "job": str(root.resolve().parent),
               "source_digest": source_digest, "target": settings.soc}
    _write_receipt(root / _BUILD_BINDING, binding)
    (root / (name + ".json")).write_text(context["project_json_src"])
    _run(["msopgen", "gen", "-i", name + ".json", "-c", settings.soc,
          "-lan", "cpp", "-out", project.name], root, build_binding=binding)
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
    if set(diagnostic_options) - {'-save-temps', '--save-temp-files'}:
        raise ValueError('Unsupported diagnostic compiler option')
    include_roots = _project_include_roots(project)
    # Device recipes additionally receive explicit -I options; host codegen
    # helpers which bypass CMake targets inherit the same roots through CPATH.
    _inject_kernel_include_paths(project, (*map(str, include_roots), *extra_kernel_include_paths), diagnostic_options)
    # Keep the established native build/deploy protocol; isolate it per job.
    _run(["./build.sh"], project, include_roots=include_roots, build_binding=binding)
    installers = sorted((project / "build_out").glob("custom_opp_*.run"))
    if len(installers) != 1:
        raise ValueError("expected one unambiguous native installer")
    _run([str(installers[0]), "--quiet", "--install-path=" + str(root / "opp")], installers[0].parent,
         include_roots=include_roots, build_binding=binding)
    _run([sys.executable, "setup.py", "build_ext", "--inplace"], root / "CppExtension",
         include_roots=include_roots, build_binding=binding)
    return capture_outputs(root, source_digest, settings.soc)
