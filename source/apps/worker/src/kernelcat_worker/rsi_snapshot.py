"""Finite immutable Git captures for owner-side RSI evaluation, after admission."""
from __future__ import annotations

import json
import os
import shutil
import stat
import time
from pathlib import Path, PurePosixPath

from kernelcat_executor.capabilities.process import collect_process
from kernelcat_executor.contracts.models import Conflict, Json, canonical, digest
from kernelcat_executor.contracts.rsi import validate_rsi


def _source_names(files: list[Json]) -> None:
    names = [item["path"] for item in files]
    if len(names) != len(set(names)):
        raise Conflict("RSI snapshot repeats a source path")
    for name in names:
        path = PurePosixPath(name)
        if (path.is_absolute() or str(path) != name or "\0" in name
                or any(part.lower() in {"..", ".git"} for part in path.parts)):
            raise Conflict("RSI snapshot requires canonical source paths outside Git metadata")


class SourceSnapshots:
    """One original effect owns this directory; existing attempts never redispatch.

    O(bytes + path bytes + files log files) capture/verification work. No candidate Git config, hooks,
    filters or working-tree evaluator files are imported into the owner checkout.
    This filesystem boundary does not claim OS isolation of arbitrary native code.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repository = root / "sources.git"
        executable = shutil.which("git")
        if executable is None:
            raise Conflict("RSI immutable evaluation requires an installed Git executable")
        self.executable = str(Path(executable).resolve())
        self.deadline = time.monotonic() + 30
        self.environment = {"HOME": str(root), "PATH": str(Path(self.executable).parent),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "KernelCat controller",
            "GIT_AUTHOR_EMAIL": "controller@invalid", "GIT_COMMITTER_NAME": "KernelCat controller",
            "GIT_COMMITTER_EMAIL": "controller@invalid", "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z", "LC_ALL": "C"}

    def _git(self, *arguments: str, data: bytes = b"", index: Path | None = None) -> str:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Conflict("RSI Git snapshot exceeded its finite deadline")
        environment = dict(self.environment)
        if index is not None:
            environment["GIT_INDEX_FILE"] = str(index)
        result = collect_process((self.executable, "-c", "core.hooksPath=" + os.devnull,
            "--git-dir=" + str(self.repository), *arguments), self.root, environment,
            data, remaining, 65536, 65536)
        if result.reason or result.returncode != 0:
            raise Conflict("RSI Git snapshot failed; original intent requires inspection: "
                           + (result.reason or result.stderr.decode(errors="replace")[:1000]))
        return result.stdout.decode().strip()

    def capture(self, arm: str, files: list[Json], *, checkout: Path | None = None) -> Json:
        _source_names(files)
        index = self.root / (arm + ".index")
        entries = []
        for item in sorted(files, key=lambda value: value["path"]):
            blob = self._git("hash-object", "-w", "--stdin", "--no-filters", data=item["text"].encode())
            entries.append(f"100644 {blob}\t{item['path']}\0")
        self._git("read-tree", "--empty", index=index)
        self._git("update-index", "-z", "--index-info", data="".join(entries).encode(), index=index)
        tree = self._git("write-tree", index=index)
        commit = self._git("commit-tree", tree, data=("RSI source " + digest(files) + "\n").encode())
        self._git("update-ref", "refs/heads/" + arm, commit)
        checkout = self.root / arm if checkout is None else checkout
        self._git("worktree", "add", "--detach", str(checkout), commit)
        return {"repository": str(self.repository), "commit": commit,
                "worktree": str(checkout), "source_digest": digest(files)}

    def branch(self, files: list[Json], workspace: Path) -> Json:
        """Create a fresh controller Git repository/worktree from admitted source bytes."""
        self.root.mkdir(parents=True, mode=0o700, exist_ok=False)
        self._git("init", "--bare", "--template=", str(self.repository))
        return self.capture("branch", files, checkout=workspace)

    def verify_branch(self, snapshot: Json, files: list[Json]) -> None:
        """Read original Git objects and checkout; reconciliation creates no commit."""
        checkout = Path(snapshot["worktree"])
        if checkout.resolve() != checkout or self.repository.resolve() != self.repository:
            raise Conflict("branch source roots changed")
        commit = snapshot["commit"]
        if self._git("rev-parse", "refs/heads/branch") != commit:
            raise Conflict("branch commit differs from its retained source")
        names = self._git("ls-tree", "-r", "--name-only", "-z", commit).split("\0")
        if sorted(name for name in names if name) != sorted(item["path"] for item in files):
            raise Conflict("branch commit source paths changed")
        for item in files:
            blob = self._git("hash-object", "--stdin", "--no-filters", data=item["text"].encode())
            if self._git("rev-parse", commit + ":" + item["path"]) != blob:
                raise Conflict("branch commit source bytes changed")
        self._verify_files(checkout, files, "branch")

    def prepare(self, request: Json) -> Json:
        self.root.mkdir(parents=True, mode=0o700, exist_ok=False)
        self._git("init", "--bare", "--template=", str(self.repository))
        snapshot: Json = {"schema_version": "kernelcat.rsi-source-snapshot/v1"}
        for arm in ("baseline", "keep", "remove"):
            snapshot[arm] = self.capture(arm, request[arm])
        self.verify(snapshot, request)
        (self.root / "driver").mkdir(mode=0o700)
        with (self.root / "snapshot.json").open("x") as stream:
            stream.write(canonical(snapshot))
            stream.flush()
            os.fsync(stream.fileno())
        return snapshot

    def verify(self, snapshot: Json, request: Json) -> None:
        for arm in ("baseline", "keep", "remove"):
            self._verify_files(Path(snapshot[arm]["worktree"]), request[arm], arm)

    def retained(self, request: Json) -> Json:
        """Validate disk metadata against the original deterministic Git capture.

        Collection cannot trust a schema-valid replacement commit or repository.
        This reads original refs/objects and computes hashes without writing any
        Git object, branch or checkout. Work is O(files + source bytes).
        """
        path = self.root / "snapshot.json"
        if path.resolve() != path or self.repository.resolve() != self.repository:
            raise Conflict("RSI retained source roots changed")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 65536:
                raise Conflict("RSI retained source manifest must be a bounded regular file")
            contents = stream.read(65537)
        if len(contents) > 65536:
            raise Conflict("RSI retained source manifest exceeds its byte limit")
        snapshot = validate_rsi(json.loads(contents), "RsiSourceSnapshot")
        for arm in ("baseline", "keep", "remove"):
            self._verify_retained_arm(snapshot[arm], request[arm], arm)
        return snapshot

    def _verify_retained_arm(self, snapshot: Json, files: list[Json], arm: str) -> None:
        expected = self.root / arm
        if (snapshot["repository"] != str(self.repository)
                or snapshot["worktree"] != str(expected) or expected.resolve() != expected
                or snapshot["source_digest"] != digest(files)):
            raise Conflict("RSI retained source provenance changed: " + arm)
        commit = snapshot["commit"]
        if self._git("rev-parse", "refs/heads/" + arm) != commit:
            raise Conflict("RSI retained source commit changed: " + arm)
        tree = self._git("rev-parse", commit + "^{tree}")
        if tree != self._tree_hash(files):
            raise Conflict("RSI retained source tree changed: " + arm)
        # capture() has no parents and fixes the author, committer and UTC date.
        identity = "KernelCat controller <controller@invalid> 946684800 +0000"
        encoded = (f"tree {tree}\nauthor {identity}\ncommitter {identity}\n\n"
                   f"RSI source {digest(files)}\n").encode()
        if self._git("hash-object", "-t", "commit", "--stdin", data=encoded) != commit:
            raise Conflict("RSI retained source commit identity changed: " + arm)
        names = self._git("ls-tree", "-r", "--name-only", "-z", commit).split("\0")
        if sorted(name for name in names if name) != sorted(item["path"] for item in files):
            raise Conflict("RSI retained source commit paths changed: " + arm)
        for item in files:
            blob = self._git("hash-object", "--stdin", "--no-filters", data=item["text"].encode())
            if self._git("rev-parse", commit + ":" + item["path"]) != blob:
                raise Conflict("RSI retained source commit bytes changed: " + arm)
        self._verify_files(expected, files, arm)

    def _tree_hash(self, files: list[Json]) -> str:
        """Hash the capture's canonical regular-file tree without writing objects.

        Exact modes, directory inventory and ordering are part of Git identity.
        O(source bytes + path bytes + F log F); no mutable retained index is trusted.
        """
        directories: dict[str, dict[str, tuple[str, str]]] = {"": {}}
        for item in files:
            parts = PurePosixPath(item["path"]).parts
            for depth in range(len(parts) - 1):
                parent = "/".join(parts[:depth])
                child = "/".join(parts[:depth + 1])
                directories.setdefault(parent, {})[parts[depth]] = ("40000", child)
                directories.setdefault(child, {})
            blob = self._git("hash-object", "--stdin", "--no-filters", data=item["text"].encode())
            directories["/".join(parts[:-1])][parts[-1]] = ("100644", blob)
        hashes: dict[str, str] = {}
        for directory in sorted(directories, key=lambda name: (len(PurePosixPath(name).parts), name), reverse=True):
            entries = directories[directory]
            names = sorted(entries, key=lambda name: (name + ("/" if entries[name][0] == "40000" else "")).encode())
            payload = bytearray()
            for name in names:
                mode, reference = entries[name]
                identity = hashes[reference] if mode == "40000" else reference
                payload.extend(mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(identity))
            hashes[directory] = self._git("hash-object", "-t", "tree", "--stdin", data=bytes(payload))
        return hashes[""]

    @staticmethod
    def _verify_files(checkout: Path, files: list[Json], arm: str) -> None:
        for item in files:
            path = checkout / item["path"]
            if path.is_symlink() or not path.is_file() or path.read_bytes() != item["text"].encode():
                raise Conflict("RSI evaluator changed its frozen input: " + arm + "/" + item["path"])
