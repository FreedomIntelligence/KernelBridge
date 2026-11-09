"""Read-only, bounded raw-source filesystem adapter."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from kernelcat_knowledge.resources.configuration import load_repository_paths
from kernelcat_knowledge.storage.legacy_index import LOADER, IndexLoader

_TEXT_EXTENSIONS = {
    "",
    ".md",
    ".txt",
    ".rst",
    ".h",
    ".hpp",
    ".hh",
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".py",
    ".yaml",
    ".yml",
    ".json",
    ".sh",
    ".cmake",
}
_TEST_PARTS = {"test", "tests", "ut", "st", "example", "examples"}


def _role(path: Path) -> str:
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    if "op_host" in parts or "tiling" in name or "host" in name:
        return "host"
    if path.suffix.lower() in {".md", ".rst", ".txt"} or "doc" in parts:
        return "doc"
    if "op_kernel" in parts or "kernel" in name:
        return "kernel"
    if "guide" in parts:
        return "guide"
    return "other"


def _grep_error(code: str, message: str) -> dict:
    return {
        "hits": [],
        "returned": 0,
        "total_lower_bound": 0,
        "truncated": False,
        "scope_resolved": [],
        "code": code,
        "error": message,
    }


def _read_error(path: str, code: str, message: str) -> dict[str, str]:
    return {"path": path, "code": code, "error": message}


class RawReader:
    def __init__(
        self,
        raw_root: Path | None = None,
        loader: IndexLoader | None = None,
        max_lines: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        if raw_root is None or max_lines is None or max_bytes is None:
            defaults = load_repository_paths()
            raw_root = raw_root or defaults.raw_root
            max_lines = max_lines if max_lines is not None else defaults.raw_max_lines
            max_bytes = max_bytes if max_bytes is not None else defaults.raw_max_bytes
        self.raw_root = raw_root.resolve()
        self.repo_root = self.raw_root.parent.resolve()
        self.loader = loader or LOADER
        self.max_lines = int(max_lines)
        self.max_bytes = int(max_bytes)

    def _safe(self, raw: str | Path) -> Path | None:
        path = Path(raw)
        candidate = path.resolve() if path.is_absolute() else (self.repo_root / path).resolve()
        try:
            candidate.relative_to(self.raw_root)
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _source_values(entry: Mapping) -> list[str]:
        values = [str(item) for item in entry.get("raw_links") or []]
        evidence = entry.get("evidence") or []
        if not isinstance(evidence, list):
            evidence = [evidence]
        for row in evidence:
            if isinstance(row, Mapping) and str(row.get("source") or "").startswith("raw/"):
                values.append(str(row["source"]))
        return values

    def _scope_roots(self, scope: str | None) -> tuple[list[Path], list[str]]:
        if not scope:
            return [self.raw_root], []
        roots: list[Path] = []
        entry = self.loader.find_by_id(scope)
        if entry:
            for raw in self._source_values(entry):
                path = self._safe(raw.split("#", 1)[0].strip())
                if path and path.exists():
                    roots.append(path if path.is_dir() else path.parent)
        if not roots:
            needle = scope.lower().replace("-", "_")
            for path in self.raw_root.rglob("*"):
                if path.is_dir() and needle in path.name.lower().replace("-", "_"):
                    roots.append(path)
                    if len(roots) >= 8:
                        break
        roots = list(dict.fromkeys(path.resolve() for path in roots))
        return (roots or [self.raw_root]), [
            path.relative_to(self.repo_root).as_posix() for path in roots
        ]

    @staticmethod
    def _files(
        roots: Iterable[Path],
        include_tests: bool,
        glob: str | None,
    ) -> Iterable[Path]:
        seen: set[Path] = set()
        for root in roots:
            candidates = [root] if root.is_file() else root.rglob(glob or "*")
            for path in candidates:
                if (
                    path in seen
                    or not path.is_file()
                    or path.suffix.lower() not in _TEXT_EXTENSIONS
                ):
                    continue
                seen.add(path)
                lowered = {part.lower() for part in path.parts}
                if not include_tests and lowered & _TEST_PARTS:
                    continue
                yield path

    def grep(
        self,
        pattern: str,
        scope: str | None = None,
        context: int = 4,
        glob: str | None = None,
        include_tests: bool = False,
        max_hits: int = 30,
    ) -> dict:
        if not pattern:
            return _grep_error("INVALID_ARGUMENT", "empty pattern")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return _grep_error("INVALID_ARGUMENT", f"invalid regex: {exc}")
        context = max(0, min(int(context), 10))
        max_hits = max(1, min(int(max_hits), 100))
        roots, scope_resolved = self._scope_roots(scope)
        hits: list[dict] = []
        scanned = 0
        truncated = False
        for path in self._files(roots, include_tests, glob):
            scanned += 1
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:4096] or len(raw) > 8 * 1024 * 1024:
                continue
            lines = raw.decode("utf-8", errors="ignore").splitlines()
            for index, line in enumerate(lines):
                if not regex.search(line):
                    continue
                start = max(0, index - context)
                end = min(len(lines), index + context + 1)
                hits.append(
                    {
                        "path": path.relative_to(self.repo_root).as_posix(),
                        "line": index + 1,
                        "snippet": "\n".join(
                            f"{n + 1}: {lines[n]}" for n in range(start, end)
                        ),
                        "role": _role(path),
                    }
                )
                if len(hits) >= max_hits:
                    truncated = True
                    break
            if truncated:
                break
        role_order = {"host": 0, "doc": 1, "guide": 2, "kernel": 3, "other": 4}
        hits.sort(
            key=lambda row: (
                role_order.get(row["role"], 9),
                row["path"],
                row["line"],
            )
        )
        payload = {
            "hits": hits,
            "returned": len(hits),
            "total_lower_bound": len(hits),
            "truncated": truncated,
            "scope_resolved": scope_resolved,
        }
        if scanned == 0:
            payload["warning"] = "scope resolved to no readable text files"
        return payload

    def read(self, path: str, start: int = 1, end: int | None = None) -> dict:
        safe = self._safe(path)
        if safe is None:
            return _read_error(path, "OUT_OF_BOUNDS", "path must stay inside raw/")
        if not safe.is_file():
            return _read_error(path, "NOT_FOUND", "file not found")
        try:
            raw = safe.read_bytes()
        except OSError as exc:
            return _read_error(path, "READ_FAILED", f"read failed: {exc}")
        if b"\x00" in raw[:4096]:
            return _read_error(
                path,
                "UNSUPPORTED_MEDIA",
                "binary file is not readable through wiki_read_raw",
            )
        lines = raw.decode("utf-8", errors="replace").splitlines()
        total = len(lines)
        start = max(1, int(start))
        requested_end = total if end is None else max(start, int(end))
        bounded_end = min(requested_end, start + self.max_lines - 1, total)
        content = "\n".join(lines[start - 1 : bounded_end])
        encoded = content.encode("utf-8")
        byte_truncated = len(encoded) > self.max_bytes
        if byte_truncated:
            content = encoded[: self.max_bytes].decode("utf-8", errors="ignore")
        return {
            "path": safe.relative_to(self.repo_root).as_posix(),
            "start": start,
            "end": bounded_end,
            "content": content,
            "total_lines": total,
            "truncated": byte_truncated
            or bounded_end < requested_end
            or bounded_end < total,
        }
