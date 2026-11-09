"""Content and trust checks for historical operator decision cards."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from kernelcat_knowledge.cards.trust import TrustValidator

_REQUIRED_SECTIONS = (
    "task",
    "applicability",
    "decision",
    "hardware_plan",
    "risks",
    "evidence",
    "body",
)
_BODY_MARKERS = ("## 决策", "## Patch 原子", "## 正确性风险", "## 验证")
# Conservative signatures of UTF-8 text that has been decoded/re-encoded through
# a Western code page. These strings should never occur in this Chinese/English
# operator-card corpus.
_MOJIBAKE_MARKERS = ("\ufffd", "â€”", "â€“", "â€", "ã€", "ï¼", "Ã", "Â")


def card_paths(path: Path) -> list[Path]:
    return sorted(path.glob("*.jsonl")) if path.is_dir() else [path]


def load_cards(path: Path) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for source in card_paths(path):
        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"{source}: UTF-8 decode failed at byte {exc.start}: {exc.reason}"
            ) from exc
        for line_no, raw in enumerate(text.splitlines(), 1):
            if not raw.strip():
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{source}:{line_no}: {exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"{source}:{line_no}: card must be an object")  # noqa: TRY004 -- contract refusal
            cards.append(item)
    return cards


def _iter_strings(value: Any, field: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield field, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_strings(item, f"{field}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_strings(item, f"{field}[{index}]")


def _text_quality_errors(item: dict[str, Any], prefix: str) -> list[str]:
    errors: list[str] = []
    for field, text in _iter_strings(item):
        for marker in _MOJIBAKE_MARKERS:
            if marker in text:
                errors.append(
                    f"{prefix}: {field} contains mojibake marker {marker!r}"
                )
        controls = sorted(
            {
                f"U+{ord(ch):04X}"
                for ch in text
                if (ord(ch) < 32 and ch not in "\t\n\r")
                or 0x7F <= ord(ch) <= 0x9F
            }
        )
        if controls:
            errors.append(
                f"{prefix}: {field} contains control characters "
                f"{', '.join(controls)}"
            )
    return errors


def validate_cards(repo_root: Path, path: Path) -> list[str]:
    errors: list[str] = []
    cards = load_cards(path)
    ids: set[str] = set()
    for index, item in enumerate(cards, 1):
        kid = str(item.get("ID") or "")
        prefix = f"line {index} ({kid or '?'})"
        if not kid or item.get("id") != kid:
            errors.append(
                f"{prefix}: id and ID must be the same non-empty value"
            )
        if kid in ids:
            errors.append(f"{prefix}: duplicate ID")
        ids.add(kid)
        if (
            item.get("type") != "operator_card"
            or item.get("trust_contract") != "v1"
        ):
            errors.append(f"{prefix}: not an opted-in operator_card")
        for field in _REQUIRED_SECTIONS:
            if item.get(field) in (None, "", [], {}):
                errors.append(f"{prefix}: missing {field}")
        body = str(item.get("body") or "")
        for marker in _BODY_MARKERS:
            if marker not in body:
                errors.append(f"{prefix}: body missing {marker}")
        content_path = repo_root / str(item.get("content_path") or "")
        if not content_path.is_file():
            errors.append(
                f"{prefix}: content_path not found: {item.get('content_path')}"
            )
        task = item.get("task") or {}
        if not isinstance(task, dict) or not task.get("operator"):
            errors.append(f"{prefix}: task.operator missing")
        decision = item.get("decision") or {}
        if not isinstance(decision, dict) or not decision.get("patch_atoms"):
            errors.append(f"{prefix}: decision.patch_atoms missing")
        errors.extend(_text_quality_errors(item, prefix))
    trust_report = TrustValidator(repo_root).validate_paths(card_paths(path))
    errors.extend(
        f"{finding.path}: {finding.code}: {finding.message}"
        for finding in trust_report.errors
    )
    return errors
