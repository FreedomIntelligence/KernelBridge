"""Read immutable source scopes and reject evidence outside the registered bytes."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from kernelcat_knowledge.contracts import (
    ContractError,
    sha256_digest,
    validate_source_record,
)
from kernelcat_knowledge.ports import RepositoryError


class SourceContentError(ValueError):
    def __init__(self, code: str, message: str, *, unsupported: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.unsupported = unsupported


class SourceFiles(Protocol):
    """Content access only; source/selector validation stays in this module."""

    def path(self, locator: str) -> Path: ...
    def read(self, path: Path) -> bytes: ...
    def is_directory(self, path: Path) -> bool: ...
    def members(self, path: Path) -> Iterable[Path]: ...


class LocalSourceFiles:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, locator: str) -> Path:
        return local_path(locator, self.root)

    def read(self, path: Path) -> bytes:
        return _file_bytes(path)

    def is_directory(self, path: Path) -> bool:
        return path.is_dir()

    def members(self, path: Path) -> Iterable[Path]:
        for member in sorted(path.rglob("*")):
            if member.is_symlink():
                raise SourceContentError(
                    "UNSUPPORTED_SOURCE", "Directory contains a symlink.", unsupported=True
                )
            if member.is_file():
                yield member


@dataclass(frozen=True)
class SelectedSourceRange:
    path: Path
    start: int
    end: int
    artifact_digest: str


@dataclass(frozen=True)
class SourceContent:
    path: Path | None
    artifact_ref: str | None
    raw: bytes | None
    digest_scope: str
    actual_digest: str
    members: tuple[tuple[str, str], ...] = ()
    selected_ranges: tuple[SelectedSourceRange, ...] = ()


def bytes_digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def local_path(locator: str, repo_root: Path) -> Path:
    parsed = urlsplit(locator)
    if parsed.scheme not in {"", "file"} or parsed.netloc not in {"", "localhost"}:
        raise SourceContentError(
            "UNSUPPORTED_SOURCE",
            "Source has no local immutable content adapter.",
            unsupported=True,
        )
    if parsed.query or parsed.fragment or "::" in locator:
        raise SourceContentError(
            "UNSUPPORTED_SELECTOR",
            "Put source selection in content_selector, not the locator.",
            unsupported=True,
        )
    value = Path(unquote(parsed.path))
    path = (value if value.is_absolute() else repo_root / value).resolve()
    if not path.is_relative_to(repo_root.resolve()):
        raise SourceContentError(
            "SOURCE_OUTSIDE_ROOT", "Source path leaves its configured repository root."
        )
    return path


def _file_bytes(path: Path) -> bytes:
    try:
        if not path.is_file() or path.is_symlink():
            raise SourceContentError(
                "SOURCE_UNAVAILABLE", "Source must be a readable regular file."
            )
        if path.stat().st_size > 16 * 1024 * 1024:
            raise SourceContentError(
                "SOURCE_LIMIT", "Source file exceeds the 16 MiB verification limit."
            )
        return path.read_bytes()
    except OSError as error:
        raise SourceContentError(
            "SOURCE_UNAVAILABLE", "Source file could not be read."
        ) from error


def _line_range(selector: Mapping[str, Any], total: int) -> tuple[int, int]:
    supported = {
        "path",
        "artifact_ref",
        "kind",
        "line_start",
        "line_end",
        "start",
        "end",
        "symbol",
        "version",
    }
    if set(selector) - supported:
        raise SourceContentError(
            "UNSUPPORTED_SELECTOR",
            "Source selector contains unsupported fields.",
            unsupported=True,
        )
    if {"start", "line_start"} <= selector.keys() or {
        "end",
        "line_end",
    } <= selector.keys():
        raise SourceContentError(
            "AMBIGUOUS_SELECTOR", "Use one spelling for each selector boundary."
        )
    if selector.get("kind", "lines") not in {"lines", "document"}:
        raise SourceContentError(
            "UNSUPPORTED_SELECTOR",
            "This reader supports document and exact line selectors.",
            unsupported=True,
        )
    start = selector.get("line_start", selector.get("start", 1))
    end = selector.get("line_end", selector.get("end", total))
    if "symbol" in selector and not any(
        key in selector for key in ("line_start", "start")
    ):
        raise SourceContentError(
            "UNSUPPORTED_SELECTOR",
            "A symbol requires an exact registered source range.",
            unsupported=True,
        )
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= total:
        raise SourceContentError(
            "SELECTOR_OUT_OF_BOUNDS",
            "Evidence line range lies outside the source content.",
        )
    return start, end


def _source_selection(
    record: Mapping[str, Any],
    path: Path,
    access: SourceFiles,
) -> tuple[str, tuple[SelectedSourceRange, ...]]:
    parts = []
    ranges = []
    selectors = record.get("content_selector")
    if not isinstance(selectors, list) or not selectors:
        raise SourceContentError(
            "UNSUPPORTED_SELECTOR",
            "Selection scope requires a non-empty selector list.",
            unsupported=True,
        )
    for selector in selectors:
        if not isinstance(selector, Mapping):
            raise SourceContentError(
                "UNSUPPORTED_SELECTOR",
                "Each source selector must be an object.",
                unsupported=True,
            )
        if selector.get("version") is not None and selector["version"] != record.get(
            "version"
        ):
            raise SourceContentError(
                "SELECTOR_SOURCE_MISMATCH", "Source selector names a different version."
            )
        selected_path = access.path(str(selector.get("path") or path))
        if (access.is_directory(path) and not selected_path.is_relative_to(path)) or (
            not access.is_directory(path) and selected_path != path
        ):
            raise SourceContentError(
                "SELECTOR_SOURCE_MISMATCH",
                "Source selector leaves the registered source.",
            )
        raw = access.read(selected_path)
        try:
            lines = raw.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError as error:
            raise SourceContentError(
                "UNREADABLE_SOURCE", "Line selectors require lossless UTF-8 decoding."
            ) from error
        start, end = _line_range(selector, len(lines))
        selected_text = "".join(lines[start - 1 : end])
        if selector.get("symbol") and str(selector["symbol"]) not in selected_text:
            raise SourceContentError(
                "SELECTOR_SOURCE_MISMATCH",
                "Registered symbol does not occur in the source range.",
            )
        selected = selected_text.encode("utf-8")
        artifact_digest = bytes_digest(raw)
        ranges.append(SelectedSourceRange(selected_path, start, end, artifact_digest))
        parts.append(
            {
                "selector": dict(selector),
                "artifact_digest": bytes_digest(raw),
                "selected_digest": bytes_digest(selected),
                "size_bytes": len(selected),
            }
        )
    digest = sha256_digest(
        {"schema_version": "kernelcat.source-selection/v1", "parts": parts}
    )
    return digest, tuple(ranges)


def verify_source_content(
    record: Mapping[str, Any],
    repo_root: Path,
    *,
    artifact_reader: Callable[[str], bytes] | None = None,
    files: SourceFiles | None = None,
) -> SourceContent:
    """Digest the declared scope. Existence or a stored status is never verification."""
    access = files if files is not None else LocalSourceFiles(repo_root)
    try:
        validate_source_record(record)
    except ContractError as error:
        raise SourceContentError("INVALID_CONTRACT", str(error)) from error
    if record.get("status") != "active":
        raise SourceContentError("SOURCE_INACTIVE", "Source revision is not active.")
    artifact_ref = record.get("artifact_ref")
    selectors = record.get("content_selector") or []
    scope = record.get("digest_scope")
    if selectors and scope not in {"selection", "file_bytes"}:
        raise SourceContentError(
            "UNSUPPORTED_SELECTOR",
            "Selected content needs an explicit supported digest_scope.",
            unsupported=True,
        )
    if artifact_ref:
        if selectors or scope not in {None, "file_bytes"}:
            raise SourceContentError(
                "UNSUPPORTED_SELECTOR",
                "Artifact sources currently require whole-file digest scope.",
                unsupported=True,
            )
        if artifact_reader is None:
            raise SourceContentError(
                "SOURCE_UNAVAILABLE",
                "No artifact reader was configured for this source.",
            )
        try:
            raw = artifact_reader(str(artifact_ref))
        except RepositoryError as error:
            raise SourceContentError(
                "SOURCE_UNAVAILABLE", "Registered source artifact is unavailable."
            ) from error
        content = SourceContent(
            None, str(artifact_ref), raw, "file_bytes", bytes_digest(raw)
        )
    else:
        path = access.path(str(record.get("locator") or ""))
        if scope == "selection":
            actual, selected_ranges = _source_selection(record, path, access)
            content = SourceContent(
                path, None, None, "selection", actual, selected_ranges=selected_ranges
            )
        elif access.is_directory(path):
            if scope != "directory_manifest":
                raise SourceContentError(
                    "UNSUPPORTED_DIGEST_SCOPE",
                    "Directories require directory_manifest digest scope.",
                    unsupported=True,
                )
            manifest_files: list[dict[str, Any]] = []
            total_bytes = 0
            for member in access.members(path):
                raw = access.read(member)
                total_bytes += len(raw)
                if len(manifest_files) >= 100_000 or total_bytes > 256 * 1024 * 1024:
                    raise SourceContentError(
                        "SOURCE_LIMIT",
                        "Directory verification exceeds its declared read limits.",
                    )
                manifest_files.append(
                    {
                        "path": member.relative_to(path).as_posix(),
                        "digest": bytes_digest(raw),
                        "size_bytes": len(raw),
                    }
                )
            actual = sha256_digest(
                {"schema_version": "kernelcat.source-tree/v1", "files": manifest_files}
            )
            content = SourceContent(
                path,
                None,
                None,
                "directory_manifest",
                actual,
                tuple((item["path"], item["digest"]) for item in manifest_files),
            )
        else:
            if scope not in {None, "file_bytes"}:
                raise SourceContentError(
                    "UNSUPPORTED_DIGEST_SCOPE",
                    "File source has an unsupported digest scope.",
                    unsupported=True,
                )
            raw = access.read(path)
            selected_ranges = ()
            if selectors:
                _, selected_ranges = _source_selection(record, path, access)
            content = SourceContent(
                path,
                None,
                raw,
                "file_bytes",
                bytes_digest(raw),
                selected_ranges=selected_ranges,
            )
    if content.actual_digest != record.get("digest"):
        raise SourceContentError(
            "SOURCE_INTEGRITY_MISMATCH",
            "Registered source digest does not match its current bytes.",
        )
    return content


def inspect_source_content(
    record: Mapping[str, Any],
    repo_root: Path,
    *,
    artifact_reader: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    try:
        content = verify_source_content(
            record, repo_root, artifact_reader=artifact_reader
        )
    except SourceContentError as error:
        return {
            "status": "unverified" if error.unsupported else "error",
            "code": error.code,
            "source_id": record["source_id"],
            "contract_valid": error.code != "INVALID_CONTRACT",
            "content_verified": False,
            "message": str(error),
        }
    return {
        "status": "verified",
        "source_id": record["source_id"],
        "contract_valid": True,
        "content_verified": True,
        "digest_scope": content.digest_scope,
        "expected_digest": record["digest"],
        "actual_digest": content.actual_digest,
    }


def read_evidence_content(
    evidence: Mapping[str, Any],
    source: Mapping[str, Any],
    repo_root: Path,
    *,
    selector_override: Mapping[str, Any] | None = None,
    max_lines: int,
    max_bytes: int,
    artifact_reader: Callable[[str], bytes] | None = None,
    files: SourceFiles | None = None,
) -> dict[str, Any]:
    access = files if files is not None else LocalSourceFiles(repo_root)
    content = verify_source_content(
        source, repo_root, artifact_reader=artifact_reader, files=access
    )
    if evidence.get("source_id") != source.get("source_id"):
        raise SourceContentError(
            "SELECTOR_SOURCE_MISMATCH", "Evidence names a different source revision."
        )
    selector = evidence.get("selector")
    if not isinstance(selector, Mapping):
        raise SourceContentError(
            "UNSUPPORTED_SELECTOR",
            "Evidence selector must be an object.",
            unsupported=True,
        )
    supported = {
        "path",
        "artifact_ref",
        "kind",
        "line_start",
        "line_end",
        "start",
        "end",
        "symbol",
        "version",
    }
    if set(selector) - supported:
        raise SourceContentError(
            "UNSUPPORTED_SELECTOR",
            "Evidence selector contains unsupported fields.",
            unsupported=True,
        )
    if selector.get("version") is not None and selector["version"] != source.get(
        "version"
    ):
        raise SourceContentError(
            "SELECTOR_SOURCE_MISMATCH", "Selector names a different source version."
        )
    if content.artifact_ref:
        if selector.get("artifact_ref") != content.artifact_ref:
            raise SourceContentError(
                "SELECTOR_SOURCE_MISMATCH",
                "Evidence artifact differs from the registered source.",
            )
        raw = content.raw
        path_label = str(content.artifact_ref)
        selected_path = None
    else:
        selected_path = access.path(str(selector.get("path") or ""))
        if content.path is None or (
            content.raw is not None and selected_path != content.path
        ):
            raise SourceContentError(
                "SELECTOR_SOURCE_MISMATCH",
                "Evidence path differs from the registered source.",
            )
        if content.raw is not None:
            raw = content.raw
        elif content.digest_scope == "directory_manifest":
            if not selected_path.is_relative_to(content.path):
                raise SourceContentError(
                    "SELECTOR_SOURCE_MISMATCH",
                    "Evidence path leaves the registered directory.",
                )
            raw = access.read(selected_path)
            expected = dict(content.members).get(
                selected_path.relative_to(content.path).as_posix()
            )
            if expected != bytes_digest(raw):
                raise SourceContentError(
                    "SOURCE_INTEGRITY_MISMATCH",
                    "Directory member changed during evidence reading.",
                )
        elif content.digest_scope == "selection":
            registered = [
                item for item in content.selected_ranges if item.path == selected_path
            ]
            if not registered:
                raise SourceContentError(
                    "SELECTOR_SOURCE_MISMATCH",
                    "Evidence file is outside the registered selection.",
                )
            raw = access.read(selected_path)
            if any(item.artifact_digest != bytes_digest(raw) for item in registered):
                raise SourceContentError(
                    "SOURCE_INTEGRITY_MISMATCH",
                    "Selected source changed during evidence reading.",
                )
        else:
            raise SourceContentError(
                "UNSUPPORTED_SELECTOR",
                "Selected source needs a matching subcontent reader.",
                unsupported=True,
            )
        path_label = selected_path.relative_to(repo_root).as_posix()
    if raw is None or bytes_digest(raw) != evidence.get("artifact_digest"):
        raise SourceContentError(
            "EVIDENCE_INTEGRITY_MISMATCH",
            "Evidence artifact digest differs from the source bytes.",
        )
    if evidence.get("integrity") != "verified":
        raise SourceContentError(
            "EVIDENCE_UNVERIFIED", "Evidence was not admitted with verified integrity."
        )
    try:
        text = raw.decode("utf-8")
        identity = evidence.get("parser_identity")
        if identity is not None and identity["recipe"] == "campaign-history/v2":
            # Registered source ranges retain their legacy line-number semantics.
            if content.selected_ranges:
                raise SourceContentError(
                    "UNSUPPORTED_SELECTOR", "Campaign v2 requires whole-file source scope.",
                    unsupported=True,
                )
            physical = text.removesuffix("\n").split("\n") if text else []
            lines = [line.removesuffix("\r") for line in physical]
        else:
            lines = text.splitlines()
    except UnicodeDecodeError as error:
        raise SourceContentError(
            "UNREADABLE_SOURCE", "Verified evidence requires lossless UTF-8 decoding."
        ) from error
    start, end = _line_range(selector, len(lines))
    if content.selected_ranges and not any(
        item.path == selected_path and item.start <= start <= end <= item.end
        for item in content.selected_ranges
    ):
        raise SourceContentError(
            "SELECTOR_SOURCE_MISMATCH",
            "Evidence range leaves the registered source selection.",
        )
    if selector.get("symbol") and str(selector["symbol"]) not in "\n".join(
        lines[start - 1 : end]
    ):
        raise SourceContentError(
            "SELECTOR_SOURCE_MISMATCH",
            "Registered symbol does not occur in the evidence range.",
        )
    if selector_override:
        if set(selector_override) - {"line_start", "line_end"}:
            raise SourceContentError(
                "INVALID_SELECTOR_OVERRIDE",
                "Pagination cannot replace the evidence selector.",
            )
        page_start = selector_override.get("line_start", start)
        page_end = selector_override.get("line_end", end)
        if (
            type(page_start) is not int
            or type(page_end) is not int
            or not start <= page_start <= page_end <= end
        ):
            raise SourceContentError(
                "INVALID_SELECTOR_OVERRIDE",
                "Pagination cannot widen the evidence range.",
            )
        start, end = page_start, page_end
    returned_end = min(end, start + max_lines - 1)
    text = "\n".join(lines[start - 1 : returned_end])
    encoded = text.encode("utf-8")
    truncated = len(encoded) > max_bytes or returned_end < end
    if len(encoded) > max_bytes:
        text = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return {
        "path": path_label,
        "start": start,
        "end": returned_end,
        "content": text,
        "total_lines": len(lines),
        "truncated": truncated,
        "integrity": "verified",
        "artifact_digest": evidence["artifact_digest"],
        "digest_scope": content.digest_scope,
        "source_id": source["source_id"],
        "evidence_id": evidence["evidence_id"],
    }
