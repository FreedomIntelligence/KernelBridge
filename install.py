"""Explicit online installation of one verified KernelAgent source delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


def manifest_files(bundle: Path) -> dict[str, dict[str, object]]:
    path = bundle / "PIN_MANIFEST.json"
    if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("manifest must be an ordinary file of at most 8 MiB")
    manifest = json.loads(path.read_bytes())
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "kernelcat.customer-source/v1"
    ):
        raise ValueError("unsupported customer source manifest")
    files = manifest.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= 12000:
        raise ValueError("invalid or oversized delivery inventory")
    for relative, entry in files.items():
        path = Path(relative)
        if (
            not relative
            or path.is_absolute()
            or path.as_posix() != relative
            or set(path.parts) & {".", ".."}
            or "\\" in relative
        ):
            raise ValueError(f"unsafe manifest path: {relative}")
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
            or type(entry.get("bytes")) is not int
            or not 0 <= entry["bytes"] <= 16 * 1024 * 1024
        ):
            raise ValueError(f"invalid file identity: {relative}")
    return files


def is_desktop_metadata(path: Path) -> bool:
    """Finder metadata is never a runtime input; links and special files still refuse."""
    return path.name == ".DS_Store" and stat.S_ISREG(path.lstat().st_mode)


def verify_bundle(bundle: Path) -> None:
    files = manifest_files(bundle)
    for relative, expected in files.items():
        path = bundle / relative
        if path.resolve(strict=True) != path or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError(f"not an ordinary delivery file: {relative}")
        data = path.read_bytes()
        if (
            len(data) != expected["bytes"]
            or hashlib.sha256(data).hexdigest() != expected["sha256"]
        ):
            raise ValueError(f"changed delivery file: {relative}")
    # Only the one declared npm dependency directory may appear after installation.
    actual = set()
    for directory, names, filenames in os.walk(bundle / "source", followlinks=False):
        for name in [*names, *filenames]:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f"source symlink is not allowed: {path}")
        if Path(directory) == bundle / "source/apps/agent":
            names[:] = [name for name in names if name != "node_modules"]
        actual.update(
            str((Path(directory) / name).relative_to(bundle)) for name in filenames
            if not is_desktop_metadata(Path(directory) / name)
        )
    if actual != {name for name in files if name.startswith("source/")}:
        raise ValueError("source inventory differs from the delivery manifest")
    example_files = {
        path.relative_to(bundle).as_posix()
        for path in (bundle / "examples").rglob("*")
        if path.is_file() and not is_desktop_metadata(path)
    }
    if example_files != {name for name in files if name.startswith("examples/")}:
        raise ValueError("example inventory differs from the delivery manifest")


def executable(raw: str, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"{label} requires an absolute executable path: {raw}")
    return path.parent.resolve(strict=True) / path.name


def installation_environment(node: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "PYTHON", "npm_config_", "NPM_CONFIG_"))
        and key not in {"NODE_OPTIONS", "VIRTUAL_ENV"}
    }
    environment.update(
        PIP_CONFIG_FILE=os.devnull,
        PYTHONDONTWRITEBYTECODE="1",
        PATH=str(node.parent) + os.pathsep + environment.get("PATH", ""),
    )
    return environment


def isolate_installation_state(
    environment: dict[str, str], runtime: Path, admission: Path
) -> None:
    for name in ("npm-user.npmrc", "npm-global.npmrc"):
        (admission / name).touch(exist_ok=False)
    environment.update(
        PIP_CACHE_DIR=str(runtime / "pip-cache"),
        npm_config_cache=str(runtime / "npm-cache"),
        npm_config_userconfig=str(admission / "npm-user.npmrc"),
        npm_config_globalconfig=str(admission / "npm-global.npmrc"),
    )


def execute(command: list[str], cwd: Path, environment: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True, timeout=600)


def install(bundle: Path, runtime: Path, python: Path, node: Path, npm: Path) -> None:
    verify_bundle(bundle)
    if runtime.exists() or runtime.is_symlink():
        raise ValueError(f"runtime already exists; select a new directory: {runtime}")
    if runtime.is_relative_to(bundle / "source") or bundle.is_relative_to(runtime):
        raise ValueError(
            "runtime must be outside delivered source and cannot contain the bundle"
        )
    for path in (runtime, python, node, npm):
        if any(character in str(path) for character in "\n\r\x00"):
            raise ValueError("installation paths cannot contain control characters")
    admission = bundle / ".installation"
    if admission.exists() or admission.is_symlink():
        raise ValueError(
            "this bundle already has an installation attempt; unpack a fresh bundle"
        )
    if (bundle / "source/apps/agent/node_modules").exists():
        raise ValueError("Node dependencies already exist; unpack a fresh bundle")
    environment = installation_environment(node)
    version = subprocess.check_output(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            "import sys; print(sys.version_info >= (3, 11))",
        ],
        text=True,
        env=environment,
    )
    if version.strip() != "True":
        raise ValueError("Python >=3.11 is required")
    version = subprocess.check_output(
        [str(node), "--version"], text=True, env=environment
    ).strip()
    if tuple(map(int, version.removeprefix("v").split("."))) < (22, 19, 0):
        raise ValueError("Node >=22.19.0 is required")
    # A bundle owns one shared npm tree, even when --runtime selects an external
    # directory. Keep failed admission so another install cannot destroy that tree.
    admission.mkdir()
    runtime.mkdir()
    isolate_installation_state(environment, runtime, admission)
    venv = runtime / "venv"
    execute([str(python), "-I", "-B", "-m", "venv", str(venv)], bundle, environment)
    installed = venv / "bin/python"
    pip = [str(installed), "-I", "-B", "-m", "pip"]
    execute(
        [
            *pip,
            "install",
            "--no-deps",
            "--no-compile",
            "--disable-pip-version-check",
            "--timeout",
            "60",
            "-r",
            str(bundle / "requirements.lock"),
        ],
        bundle,
        environment,
    )
    source = bundle / "source"
    helper = source / "scripts/python_deployment.py"
    deployment = runtime / "deployment"
    execute(
        [
            str(installed),
            "-I",
            "-B",
            str(helper),
            "--repository",
            str(source),
            "--output",
            str(deployment),
        ],
        bundle,
        environment,
    )
    wheels = sorted(deployment.glob("wheels/*/*.whl"))
    if len(wheels) != 4:
        raise ValueError("expected four independently built product wheels")
    execute(
        [*pip, "install", "--no-deps", "--no-compile", "--no-index", *map(str, wheels)],
        bundle,
        environment,
    )
    execute([*pip, "check"], bundle, environment)
    execute(
        [
            str(installed),
            "-I",
            "-B",
            str(helper),
            "--repository",
            str(source),
            "--deployment",
            str(deployment),
            "--installed-python",
            str(installed),
            "--output",
            str(runtime / "expected.json"),
        ],
        bundle,
        environment,
    )
    execute(
        [str(npm), "ci", "--include=dev", "--ignore-scripts", "--no-audit", "--no-fund"],
        source / "apps/agent",
        environment,
    )
    (admission / "node").write_text(str(node) + "\n")
    (admission / "runtime").write_text(str(runtime) + "\n")
    with (admission / "ready").open("x") as ready:
        ready.write("ready\n")
    print(f"Installed. Runtime: {runtime}\nRun {bundle / 'kernelagent'} --help")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--bundle", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--runtime")
    parser.add_argument("--python")
    parser.add_argument("--node")
    parser.add_argument("--npm")
    args = parser.parse_args()
    try:
        bundle = Path(args.bundle).resolve(strict=True)
        if args.verify_only:
            verify_bundle(bundle)
            print("Source delivery verified")
            return 0
        if not all((args.python, args.node, args.npm)):
            raise ValueError(
                "--python ABS, --node ABS and --npm ABS are required for installation"
            )
        raw_runtime = Path(args.runtime) if args.runtime else bundle / "runtime"
        if not raw_runtime.is_absolute():
            raise ValueError("--runtime must be absolute")
        runtime = raw_runtime.parent.resolve(strict=True) / raw_runtime.name
        install(
            bundle,
            runtime,
            executable(args.python, "--python"),
            executable(args.node, "--node"),
            executable(args.npm, "--npm"),
        )
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"install: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
