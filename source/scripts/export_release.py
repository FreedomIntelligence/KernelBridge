"""Export a committed runtime/install tree into a new directory, without changing Git refs.

    python3 -B source/scripts/export_release.py --ref HEAD --output /absolute/new-bundle

Both content and publication rules come from the selected commit. Working-tree
edits, ignored caches and untracked files cannot enter the exported bundle.
"""
from __future__ import annotations

import argparse
import copy
import fnmatch
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[2]
RULES = "source/scripts/release/rules.json"


def git(repository: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repository), *args], stderr=subprocess.PIPE)


def ordinary_path(raw: str) -> str:
    path = PurePosixPath(raw)
    if (not raw or path.is_absolute() or path.as_posix() != raw or
            any(part in {".", ".."} for part in path.parts) or "\\" in raw or "\x00" in raw):
        raise ValueError(f"unsafe release path: {raw!r}")
    return raw


def selected(path: str, rules: dict) -> bool:
    if path in rules["exclude_files"] or any(path.startswith(prefix) for prefix in rules["exclude_prefixes"]):
        return False
    if path not in rules["keep_files"] and any(fnmatch.fnmatchcase(path, pattern) for pattern in rules["exclude_patterns"]):
        return False
    return path in rules["root_files"] or any(path.startswith(prefix) for prefix in rules["include_prefixes"])


def load_rules(files: dict[str, tuple[bytes, int]]) -> dict:
    rules = json.loads(files[RULES][0])
    if rules.get("schema_version") != 1:
        raise ValueError("unsupported release rules")
    for key in ("root_files", "exclude_files", "include_prefixes", "exclude_prefixes", "keep_files"):
        if not isinstance(rules.get(key), list) or not all(isinstance(item, str) for item in rules[key]):
            raise ValueError(f"invalid release rule: {key}")
        for item in rules[key]:
            ordinary_path(item.rstrip("/"))
            if key.endswith("prefixes") and not item.endswith("/"):
                raise ValueError(f"release prefixes must end with /: {item}")
    for target, template in rules["overlays"].items():
        ordinary_path(target)
        ordinary_path(template)
        if not selected(target, rules) or not template.startswith("source/scripts/release/templates/"):
            raise ValueError(f"invalid release overlay: {target}")
        if template not in files:
            raise ValueError(f"missing committed release template: {template}")
    return rules


def read_commit(repository: Path, ref: str) -> tuple[str, dict[str, tuple[bytes, int]]]:
    commit = git(repository, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    entries = []
    for record in git(repository, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if not record:
            continue
        identity, raw_name = record.split(b"\t", 1)
        mode, kind, oid = identity.split()
        name = ordinary_path(raw_name.decode("utf-8"))
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise ValueError(f"release source contains a link, submodule or special entry: {name}")
        entries.append((name, oid, 0o755 if mode == b"100755" else 0o644))
    # Read blobs directly: git archive attributes must not silently remove a
    # required runtime file or substitute bytes from another context.
    content = subprocess.run(["git", "-C", str(repository), "cat-file", "--batch"],
                             input=b"".join(oid + b"\n" for _, oid, _ in entries),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    stream = io.BytesIO(content)
    files = {}
    for name, oid, mode in entries:
        header = stream.readline().rstrip(b"\n").split()
        if len(header) != 3 or header[:2] != [oid, b"blob"]:
            raise ValueError(f"invalid Git blob response: {name}")
        size = int(header[2])
        data = stream.read(size)
        if len(data) != size or stream.read(1) != b"\n":
            raise ValueError(f"incomplete Git blob response: {name}")
        files[name] = (data, mode)
    if RULES not in files:
        raise ValueError("selected commit has no release rules; commit the development release configuration first")
    return commit, files


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def filter_runtime_resources(output: Path) -> None:
    path = output / "source/runtime-resources.json"
    resources = json.loads(path.read_text())
    resources["source_files"] = [name for name in resources["source_files"] if (output / "source" / ordinary_path(name)).is_file()]
    executor = output / "source/packages/executor/src/kernelcat_executor"
    resources["executor_package_data"] = [name for name in resources["executor_package_data"] if (executor / ordinary_path(name)).is_file()]
    write_json(path, resources)
    path = output / "source/packages/executor/native-resources.json"
    write_json(path, [name for name in json.loads(path.read_text()) if (executor / ordinary_path(name)).is_file()])


def strip_development_package(output: Path, rules: dict) -> None:
    path = output / "source/apps/agent/package.json"
    package = json.loads(path.read_text())
    for name in rules["remove_npm_scripts"]:
        package.get("scripts", {}).pop(name, None)
    package.pop("devDependencies", None)
    write_json(path, package)
    path = output / "source/apps/agent/package-lock.json"
    lock = json.loads(path.read_text())
    if lock.get("lockfileVersion") != 3:
        raise ValueError("release export requires npm lockfile v3")
    lock["packages"].get("", {}).pop("devDependencies", None)
    lock["packages"] = {name: value for name, value in lock["packages"].items() if value.get("dev") is not True}
    write_json(path, lock)
    path = output / "source/packages/pi-lattice-trace/package.json"
    package = json.loads(path.read_text())
    package["files"] = [name for name in package["files"] if (path.parent / ordinary_path(name.rstrip("/"))).exists()]
    write_json(path, package)


def strip_python_development(output: Path) -> None:
    """Remove development TOML settings while checking every retained value."""
    table_header = re.compile(r"^\s*\[\[?([A-Za-z0-9_.-]+)\]\]?\s*(?:#.*)?$")
    development = {"pytest", "ruff", "mypy"}
    for relative in ("source/apps/worker", "source/packages/executor", "source/packages/ingestion", "source/packages/knowledge"):
        path = output / relative / "pyproject.toml"
        original = path.read_text()
        expected = copy.deepcopy(tomllib.loads(original))
        for tool in development:
            expected.get("tool", {}).pop(tool, None)
        expected.get("project", {}).get("optional-dependencies", {}).pop("dev", None)
        lines = original.splitlines(keepends=True)
        retained = []
        section, skip, index = "", False, 0
        while index < len(lines):
            line = lines[index]
            header = table_header.match(line)
            if header:
                section = header.group(1)
                parts = section.split(".")
                skip = len(parts) >= 2 and parts[0] == "tool" and parts[1] in development
            if section == "project.optional-dependencies" and re.match(r'''\s*(?:dev|"dev"|'dev')\s*=''', line):
                assignment = line
                while True:
                    try:
                        tomllib.loads(assignment)
                        break
                    except tomllib.TOMLDecodeError:
                        index += 1
                        if index >= len(lines):
                            raise ValueError(f"cannot isolate development dependencies: {relative}")
                        assignment += lines[index]
            elif not skip:
                retained.append(line)
            index += 1
        rewritten = "".join(retained).rstrip() + "\n"
        if tomllib.loads(rewritten) != expected:
            raise ValueError(f"release TOML transformation changed runtime metadata: {relative}")
        path.write_text(rewritten)


def refresh_manifest(output: Path, files: dict[str, tuple[bytes, int]], root_files: list[str], commit: str) -> None:
    # Run the exact generator from the same commit. It lives outside the bundle.
    with tempfile.TemporaryDirectory(prefix="kernelbridge-manifest-") as temporary:
        script = Path(temporary) / "update_manifest.py"
        script.write_bytes(files["source/scripts/update_manifest.py"][0])
        subprocess.run([sys.executable, "-I", "-B", str(script), "--root", str(output), "--release", "--source-commit", commit, *root_files],
                       check=True, stdout=subprocess.DEVNULL)


def export(repository: Path, ref: str, output: Path) -> dict:
    if output.exists() or output.is_symlink():
        raise ValueError(f"output must not already exist: {output}")
    output = output.parent.resolve(strict=True) / output.name
    commit, files = read_commit(repository, ref)
    rules = load_rules(files)
    chosen = {name: data for name, data in files.items() if selected(name, rules)}
    for name in rules["root_files"]:
        if name not in chosen:
            raise ValueError(f"missing required release file: {name}")
    for target, template in rules["overlays"].items():
        chosen[target] = (files[template][0], chosen[target][1])
    # Exclusive creation is the only admission point. Never replace, clean or
    # merge into a directory belonging to an earlier attempt or another process.
    output.mkdir()
    for name, (data, mode) in sorted(chosen.items()):
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
        target.chmod(mode)
    filter_runtime_resources(output)
    strip_development_package(output, rules)
    strip_python_development(output)
    refresh_manifest(output, files, rules["root_files"], commit)
    subprocess.run([sys.executable, "-I", "-B", str(output / "install.py"), "--verify-only"],
                   check=True, stdout=subprocess.DEVNULL)
    return {"source_commit": commit, "output": str(output), "files": len(chosen) + 1, "verified": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="committed source revision; working-tree edits are ignored")
    parser.add_argument("--output", required=True, type=Path, help="new bundle directory; parent must exist")
    args = parser.parse_args()
    try:
        result = export(ROOT, args.ref, args.output.absolute())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Release export failed: {error}. Any partial output is retained; select a new output directory.", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
