"""Publication boundaries: a committed runtime tree without development state."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_export", ROOT / "source/scripts/export_release.py")
assert spec and spec.loader
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class ReleaseExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kb-release-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Release Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        rules = json.loads((ROOT / exporter.RULES).read_text())
        self.write(exporter.RULES, rules)
        for name in rules["root_files"]:
            self.write(name, (ROOT / name).read_text())
        for target, template in rules["overlays"].items():
            self.write(target, "development document\n")
            self.write(template, (ROOT / template).read_text())
        self.write("source/scripts/update_manifest.py", (ROOT / "source/scripts/update_manifest.py").read_text())
        for directory in ("source/apps/worker", "source/packages/executor", "source/packages/ingestion", "source/packages/knowledge"):
            self.write(directory + "/pyproject.toml", (ROOT / directory / "pyproject.toml").read_text())
        self.write("source/scripts/develop.py", "# developer tool\n")
        self.write("source/runtime.py", "VALUE = 'committed'\n")
        self.write("source/packages/executor/src/kernelcat_executor/native.py", "# runtime\n")
        self.write("source/runtime-resources.json", {
            "schema_version": "kernelcat.customer-resources/v1",
            "source_files": ["runtime.py", "scripts/develop.py"],
            "executor_package_data": ["native.py", "removed.py"],
        })
        self.write("source/packages/executor/native-resources.json", ["native.py", "removed.py"])
        self.write("source/apps/agent/package.json", {
            "name": "fixture", "version": "1.0.0", "scripts": {"start": "node src/main.ts", "test": "node validation/test.mjs"},
            "dependencies": {"runtime": "1.0.0"}, "devDependencies": {"typescript": "5.9.3"},
        })
        self.write("source/apps/agent/package-lock.json", {
            "lockfileVersion": 3, "packages": {
                "": {"dependencies": {"runtime": "1.0.0"}, "devDependencies": {"typescript": "5.9.3"}},
                "node_modules/runtime": {"version": "1.0.0"},
                "node_modules/typescript": {"version": "5.9.3", "dev": True},
            },
        })
        self.write("source/packages/pi-lattice-trace/package.json", {"files": ["src/", "README.md"]})
        self.write("source/packages/pi-lattice-trace/src/runtime.ts", "export {};\n")
        self.write("source/packages/pi-lattice-trace/README.md", "development notes\n")
        self.write("examples/cann/adapters/native_rsi_adapter.py", "# runtime adapter\n")
        self.write("examples/cann/instructions/AGENTS.md", "Runtime instructions.\n")
        self.write("validation/private-session.json", {"development": True})
        self.write("docs/architecture/plan.md", "Development plan.\n")
        self.write("PIN_MANIFEST.json", {"derivation": {"internal": "old history"}})
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo), *args], stderr=subprocess.PIPE)

    def write(self, relative, data):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))

    def test_committed_runtime_export_is_reproducible_and_complete(self):
        self.write("source/runtime.py", "VALUE = 'uncommitted'\n")
        self.write("source/credentials.json", '{"do_not_ship": true}')
        first, second = self.base / "first", self.base / "second"
        result = exporter.export(self.repo, "HEAD", first)
        exporter.export(self.repo, "HEAD", second)
        self.assertTrue(result["verified"])
        self.assertEqual((first / "source/runtime.py").read_text(), "VALUE = 'committed'\n")
        for name in ("source/credentials.json", "source/scripts/develop.py", "docs", "validation", ".development", "source/scripts/release"):
            self.assertFalse((first / name).exists(), name)
        self.assertTrue((first / "examples/cann/adapters/native_rsi_adapter.py").is_file())
        self.assertTrue((first / "examples/cann/instructions/AGENTS.md").is_file())
        manifest = json.loads((first / "PIN_MANIFEST.json").read_text())
        self.assertNotIn("derivation", manifest)
        self.assertEqual(manifest["source"]["commit"], self.git("rev-parse", "HEAD").decode().strip())
        for name, identity in manifest["files"].items():
            data = (first / name).read_bytes()
            self.assertEqual(identity, {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.assertEqual({p.relative_to(first).as_posix(): p.read_bytes() for p in first.rglob("*") if p.is_file()},
                         {p.relative_to(second).as_posix(): p.read_bytes() for p in second.rglob("*") if p.is_file()})
        resources = json.loads((first / "source/runtime-resources.json").read_text())
        self.assertEqual(resources["source_files"], ["runtime.py"])
        self.assertEqual(resources["executor_package_data"], ["native.py"])
        package = json.loads((first / "source/apps/agent/package.json").read_text())
        self.assertEqual(package["scripts"], {"start": "node src/main.ts"})
        self.assertNotIn("devDependencies", package)
        lock = json.loads((first / "source/apps/agent/package-lock.json").read_text())
        self.assertNotIn("node_modules/typescript", lock["packages"])
        self.assertIn("node_modules/runtime", lock["packages"])
        self.assertEqual(json.loads((first / "source/packages/pi-lattice-trace/package.json").read_text())["files"], ["src/"])
        for directory in ("source/apps/worker", "source/packages/executor", "source/packages/ingestion", "source/packages/knowledge"):
            before = tomllib.loads((ROOT / directory / "pyproject.toml").read_text())
            after = tomllib.loads((first / directory / "pyproject.toml").read_text())
            self.assertEqual(before["build-system"], after["build-system"])
            self.assertEqual(before["tool"]["setuptools"], after["tool"]["setuptools"])
            self.assertEqual(before["project"]["dependencies"], after["project"]["dependencies"])
            self.assertFalse({"pytest", "ruff", "mypy"} & after["tool"].keys())
            if directory.endswith("executor"):
                self.assertEqual(before["project"]["optional-dependencies"]["backend"], after["project"]["optional-dependencies"]["backend"])
                self.assertNotIn("dev", after["project"]["optional-dependencies"])

    def test_existing_output_is_never_touched(self):
        output = self.base / "occupied"
        output.mkdir()
        (output / "keep").write_text("original")
        with self.assertRaisesRegex(ValueError, "must not already exist"):
            exporter.export(self.repo, "HEAD", output)
        self.assertEqual(list(output.iterdir()), [output / "keep"])
        self.assertEqual((output / "keep").read_text(), "original")

    def test_source_symlink_is_rejected_before_creating_output(self):
        (self.repo / "source/link").symlink_to("runtime.py")
        self.git("add", "source/link")
        self.git("commit", "-qm", "link")
        output = self.base / "unsafe"
        with self.assertRaisesRegex(ValueError, "link, submodule or special entry"):
            exporter.export(self.repo, "HEAD", output)
        self.assertFalse(output.exists())

    def test_explicit_ref_uses_that_commits_rules_and_content(self):
        original = self.git("rev-parse", "HEAD").decode().strip()
        self.write("source/runtime.py", "VALUE = 'later'\n")
        self.git("add", "source/runtime.py")
        self.git("commit", "-qm", "later")
        output = self.base / "old"
        result = exporter.export(self.repo, original, output)
        self.assertEqual(result["source_commit"], original)
        self.assertEqual((output / "source/runtime.py").read_text(), "VALUE = 'committed'\n")

    def test_archive_attributes_do_not_drop_committed_runtime_files(self):
        self.write(".gitattributes", "source/runtime.py export-ignore\n")
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "archive attributes")
        output = self.base / "attributes"
        exporter.export(self.repo, "HEAD", output)
        self.assertEqual((output / "source/runtime.py").read_text(), "VALUE = 'committed'\n")

    def test_standalone_manifest_refresh_keeps_root_runtime_identity(self):
        output = self.base / "refresh"
        exporter.export(self.repo, "HEAD", output)
        subprocess.run([sys.executable, "-I", "-B", str(ROOT / "source/scripts/update_manifest.py"),
                        "--root", str(output), "--release"], check=True, stdout=subprocess.DEVNULL)
        manifest = json.loads((output / "PIN_MANIFEST.json").read_text())
        for name in (".gitignore", "README.md", "install.py", "kernelagent", "requirements.lock", "restore.py", "restore.sh"):
            self.assertEqual(manifest["files"][name]["sha256"], hashlib.sha256((output / name).read_bytes()).hexdigest())
        self.assertFalse((output / ".development").exists())


if __name__ == "__main__":
    unittest.main()
