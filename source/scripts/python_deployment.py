"""Build one explicit, frozen four-package deployment without installing anything."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

PACKAGES = {
    "kernelcat_executor": "packages/executor",
    "kernelcat_ingestion": "packages/ingestion",
    "kernelcat_knowledge": "packages/knowledge",
    "kernelcat_worker": "apps/worker",
}
GENERATED_DIRECTORIES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


@dataclass(frozen=True)
class PythonDeployment:
    import_root: Path
    binding_path: Path
    agent_root: Path


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _source_inventory(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Audit ordinary Python bytes and link identities without following links."""
    sources: dict[str, str] = {}
    links: dict[str, str] = {}
    for directory, names, files in os.walk(root, followlinks=False):
        for name in [*names, *files]:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                links[relative] = os.readlink(path)
            elif path.suffix == ".py":
                if not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError(f"nonregular Python source: {path}")
                sources[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return sources, links


def _snapshot(package: Path, destination: Path) -> None:
    def ignored(directory: str, names: list[str]) -> set[str]:
        excluded = {name for name in names if name in GENERATED_DIRECTORIES
                    or Path(name).suffix in {".pyc", ".pyo"}}
        here = Path(directory)
        if here == package:
            excluded.update({"build", "dist"}.intersection(names))
        if here in {package, package / "src"}:
            excluded.update(name for name in names if name.endswith(".egg-info"))
        return excluded

    shutil.copytree(package, destination, symlinks=True, ignore=ignored)


def _require_matching_sources(expected: dict[str, str], actual: dict[str, str], label: str) -> None:
    if expected != actual:
        missing = sorted(expected.keys() - actual.keys())
        added = sorted(actual.keys() - expected.keys())
        changed = sorted(name for name in expected.keys() & actual.keys()
                         if expected[name] != actual[name])
        raise ValueError(f"{label}: Python payload mismatch; missing={missing}, "
                         f"added={added}, changed={changed}")


def _require_package_payload(expected: dict[str, str], protected_links: dict[str, str],
                             root: Path, label: str) -> None:
    payload, links = _source_inventory(root)
    _require_matching_sources(expected, payload, label)
    if links or any((root / name).exists() for name in protected_links):
        raise ValueError(f"{label}: wheel contains protected linked resources")


def _archive_path(entry: zipfile.ZipInfo) -> PurePosixPath:
    name = entry.filename.removesuffix("/")
    path = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or path.is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise ValueError(f"unsafe wheel path: {entry.filename}")
    mode = entry.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind not in {0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG}:
        raise ValueError(f"unsafe wheel link or special file: {entry.filename}")
    return path


def extract_wheel(wheel: Path, destination: Path) -> None:
    """Extract ordinary files, refusing traversal, links and cross-wheel collisions."""
    with zipfile.ZipFile(wheel) as archive:
        entries = [(entry, _archive_path(entry)) for entry in archive.infolist()]
        names = [path.as_posix() for _, path in entries]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate wheel member: {wheel}")
        for entry, relative in entries:
            target = destination.joinpath(*relative.parts)
            if target.resolve().is_relative_to(destination.resolve()) is False:
                raise ValueError(f"wheel path escapes destination: {relative}")
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if entry.external_attr >> 16 & 0o111 else 0o644)


def _build_wheel(snapshot: Path, wheels: Path, log: Path, interpreter: str) -> list[str]:
    command = [interpreter, "-B", "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
               "--no-index", "--no-cache-dir", "--disable-pip-version-check",
               "--wheel-dir", str(wheels), str(snapshot)]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONPYCACHEPREFIX", None)
    environment.update(PYTHONDONTWRITEBYTECODE="1", PIP_CONFIG_FILE=os.devnull)
    with log.open("xb") as output:
        result = subprocess.run(command, cwd=snapshot, env=environment,
                                stdout=output, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise ValueError(f"offline wheel build failed ({result.returncode}); see {log}; "
                         "required build tooling must already be installed")
    return command


def _package_payload(repository: Path, destination: Path, module: str,
                     relative: str, interpreter: str) -> dict[str, Any]:
    package = repository / relative
    source_root = package / "src" / module
    original, links = _source_inventory(source_root)
    snapshot = destination / "snapshots" / module
    _snapshot(package, snapshot)
    copied, copied_links = _source_inventory(snapshot / "src" / module)
    _require_matching_sources(original, copied, f"{module} snapshot")
    if links != copied_links:
        raise ValueError(f"{module}: snapshot changed protected link identities")
    wheels = destination / "wheels" / module
    wheels.mkdir(parents=True)
    command = _build_wheel(snapshot, wheels, destination / f"{module}-build.log", interpreter)
    artifacts = list(wheels.glob("*.whl"))
    if len(artifacts) != 1:
        raise ValueError(f"{module}: expected exactly one wheel, found {len(artifacts)}")
    extract_wheel(artifacts[0], destination / "unpacked")
    _require_package_payload(original, links, destination / "unpacked" / module, f"{module} wheel")
    _require_matching_sources(original, _source_inventory(snapshot / "src" / module)[0],
                              f"{module} snapshot after build")
    return {"module": module, "source": relative, "python_sources": original,
            "protected_links": links, "command": command,
            "wheel": str(artifacts[0].relative_to(destination)),
            "wheel_sha256": hashlib.sha256(artifacts[0].read_bytes()).hexdigest()}


def _binding(repository: Path, import_root: Path) -> object:
    # The reviewed bootstrap is selected by path before any product import.
    bootstrap = runpy.run_path(str(repository / "apps/agent/python/generated_startup.py"),
                               run_name="_deployment_binding_rules")
    source_digest = cast(Callable[[Path], str], bootstrap["source_digest"])
    normalize = cast(Callable[[object, tuple[str, ...]], object], bootstrap["normalize_expected"])
    modules = [{"module_name": name, "distribution_name": name.replace("_", "-"),
                "hash_rule": "python-source-tree/v1", "content_digest": source_digest(import_root / name),
                "allowed_package_roots": [(import_root / name).as_uri()]} for name in PACKAGES]
    return normalize({"schema_version": "kernelcat.python-module-binding/v1", "modules": modules},
                     tuple(PACKAGES))


def _freeze_tree(root: Path) -> None:
    for directory, _, names in os.walk(root, topdown=False):
        for name in names:
            path = Path(directory) / name
            path.chmod(path.stat().st_mode & ~0o222)
        Path(directory).chmod(0o555)


def prepare_deployment(repository: Path, destination: Path, *,
                       interpreter: str = sys.executable) -> PythonDeployment:
    """Create externally, compare checkout/snapshot/wheel bytes, bind, then freeze.

    A failed preparation remains at its destination for diagnosis. Callers must use
    a new destination, never repair an admitted deployment or retry it implicitly.
    """
    repository = repository.resolve(strict=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"deployment destination already exists: {destination}")
    destination = destination.resolve()
    if destination.is_relative_to(repository) or repository.is_relative_to(destination):
        raise ValueError("deployment must be outside the repository")
    destination.mkdir(parents=True, exist_ok=False)
    import_root = destination / "unpacked"
    import_root.mkdir()
    records = [_package_payload(repository, destination, name, relative, interpreter)
               for name, relative in PACKAGES.items()]
    for record in records:
        current, links = _source_inventory(repository / record["source"] / "src" / record["module"])
        _require_matching_sources(record["python_sources"], current, f"{record['module']} checkout")
        if record["protected_links"] != links:
            raise ValueError(f"{record['module']}: checkout link identities changed during build")
        # A later wheel can add a noncolliding member to an earlier package.
        _require_package_payload(record["python_sources"], record["protected_links"],
                                 import_root / record["module"], f"{record['module']} final deployment")
    expected_sources = {f"{record['module']}/{name}": digest
                        for record in records for name, digest in record["python_sources"].items()}
    _require_matching_sources(expected_sources, _source_inventory(import_root)[0],
                              "final deployment")
    binding_path = destination / "expected.json"
    binding_path.write_bytes(_canonical(_binding(repository, import_root)))
    (destination / "build-results.json").write_bytes(_canonical({"packages": records}))
    _freeze_tree(import_root)
    _freeze_tree(destination / "wheels")
    (destination / "build-results.json").chmod(0o444)
    binding_path.chmod(0o444)
    return PythonDeployment(import_root, binding_path, repository / "apps/agent")


def _installed_members(wheel: Path) -> dict[str, str]:
    """Known wheel payload bytes, excluding RECORD which pip rewrites at install."""
    with zipfile.ZipFile(wheel) as archive:
        return {_archive_path(entry).as_posix(): hashlib.sha256(archive.read(entry)).hexdigest()
                for entry in archive.infolist()
                if not entry.is_dir() and not entry.filename.endswith(".dist-info/RECORD")}


def _validate_installed_payload(deployment: PythonDeployment, root: Path) -> None:
    directory = deployment.binding_path.parent
    records = json.loads((directory / "build-results.json").read_bytes())["packages"]
    if [record["module"] for record in records] != list(PACKAGES):
        raise ValueError("deployment inventories must contain the four selected packages")
    for record in records:
        wheel = directory / record["wheel"]
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != record["wheel_sha256"]:
            raise ValueError(f"changed deployment wheel: {wheel}")
        members = _installed_members(wheel)
        for name, expected in members.items():
            path = root / name
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"installed wheel member mismatch: {path}")
        module = record["module"]
        package = root / module
        if package.is_symlink() or package.resolve(strict=True) != package:
            raise ValueError(f"installed package root is not canonical: {package}")
        _require_package_payload(record["python_sources"], record["protected_links"],
                                 package, f"{module} installed payload")
        actual = {path.relative_to(root).as_posix() for path in package.rglob("*") if path.is_file()}
        expected = {name for name in members if name.startswith(module + "/")}
        if actual != expected:
            raise ValueError(f"installed product payload differs: {module}; extra={sorted(actual - expected)}")


def _probe_output_text(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output if output is not None else "<not captured>"


def bind_installed(deployment: PythonDeployment, target_python: Path, output: Path) -> PythonDeployment:
    """Validate independently built bytes at an isolated interpreter's explicit root.

    The probe body uses only stdlib sysconfig; interpreter/site hooks retain the
    existing trusted-interpreter prerequisite. This helper performs no product
    import, source fallback, installation, cache removal or rejection repair.
    """
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"installed binding already exists: {output}")
    repository = deployment.agent_root.parents[1]
    output = output.resolve()
    if output.is_relative_to(repository) or repository.is_relative_to(output):
        raise ValueError("installed binding must be outside the repository")
    target_python = target_python.parent.resolve(strict=True) / target_python.name
    try:
        result = subprocess.run([str(target_python), "-I", "-B", "-c",
                                 "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                cwd=output.parent, capture_output=True, text=True, check=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"installed target sysconfig query failed for {target_python} (exit {exc.returncode})\n"
            f"stdout:\n{exc.stdout}\nstderr:\n{exc.stderr}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"installed target sysconfig query timed out for {target_python} "
            f"after {exc.timeout} seconds (target exit status unknown)\n"
            f"stdout:\n{_probe_output_text(exc.stdout)}\nstderr:\n{_probe_output_text(exc.stderr)}"
        ) from exc
    root = Path(result.stdout.strip())
    if not root.is_absolute() or root.is_symlink() or root.resolve(strict=True) != root:
        raise ValueError(f"installed site-packages root is not canonical: {root}")
    if root.is_relative_to(repository) or root == deployment.import_root:
        raise ValueError("installed target must be independent of checkout and unpacked deployment")
    if any(output.is_relative_to(root / name) for name in PACKAGES):
        raise ValueError("installed binding must be outside installed product directories")
    _validate_installed_payload(deployment, root)
    expected = json.loads(deployment.binding_path.read_bytes())
    observed = _binding(repository, root)
    for entry in expected["modules"]:
        entry["allowed_package_roots"] = [(root / entry["module_name"]).as_uri()]
    if observed != expected:
        raise ValueError("installed binding content differs from independently prepared expectation")
    # Reject all mismatches before freezing or publishing the create-only binding.
    for name in PACKAGES:
        _freeze_tree(root / name)
    with output.open("xb") as stream:
        stream.write(_canonical(expected))
    output.chmod(0o444)
    return PythonDeployment(root, output, deployment.agent_root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--installed-python", type=Path)
    parser.add_argument("--deployment", type=Path)
    options = parser.parse_args()
    if bool(options.installed_python) != bool(options.deployment):
        parser.error("--installed-python and --deployment must be supplied together")
    if options.installed_python:
        directory = options.deployment.resolve(strict=True)
        prepared = PythonDeployment(directory / "unpacked", directory / "expected.json",
                                    options.repository.resolve(strict=True) / "apps/agent")
        deployment = bind_installed(prepared, options.installed_python, options.output)
    else:
        deployment = prepare_deployment(options.repository, options.output)
    print(json.dumps({"import_root": str(deployment.import_root),
                      "binding_path": str(deployment.binding_path),
                      "agent_root": str(deployment.agent_root)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
