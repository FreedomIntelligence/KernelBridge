"""Explicit resource roots and repository path configuration; no application startup."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class RepositoryConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepositoryPaths:
    repo_root: Path
    config_path: Path
    wiki_root: Path
    static_index_path: Path
    dynamic_index_path: Path
    card_index_path: Path | None
    rule_index_path: Path | None
    nav_index_path: Path
    raw_root: Path
    raw_max_lines: int
    raw_max_bytes: int
    trajectory_delta_dir: Path
    source_records_dir: Path
    evidence_cas_dir: Path
    evidence_review_dir: Path


def _substitute_env(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        name = value[2:-1]
        if name and name.replace("_", "").isalnum():
            return os.environ.get(name, "")
    if isinstance(value, dict):
        return {key: _substitute_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute_env(item) for item in value]
    return value


def _as_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RepositoryConfigError(f"config.yaml field {field!r} must be a mapping")
    return value


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def configured_resource_root(explicit: Path | str | None = None) -> Path:
    """Resolve only caller configuration; package location never identifies resources."""
    selected = (
        explicit if explicit is not None else os.environ.get("KERNELCAT_WIKI_ROOT")
    )
    if selected is None or not str(selected).strip():
        raise RepositoryConfigError(
            "Knowledge resource root is required; pass repo_root or KERNELCAT_WIKI_ROOT"
        )
    return Path(selected).resolve()


def find_repository_root(explicit: Path | None = None) -> Path:
    root = configured_resource_root(explicit)
    if (root / "config.yaml").is_file() and (root / "wiki").is_dir():
        return root
    raise RepositoryConfigError(
        f"Knowledge resource root {root} requires config.yaml and wiki/"
    )


def load_repository_paths(repo_root: Path | None = None) -> RepositoryPaths:
    root = configured_resource_root(repo_root)
    config_path = root / "config.yaml"
    try:
        payload = (
            yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if config_path.exists()
            else {}
        )
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RepositoryConfigError(f"config.yaml read/parse failed: {exc}") from exc
    config = _as_mapping(_substitute_env(payload), "$")
    wiki = _as_mapping(config.get("wiki", {}), "wiki")
    raw_cfg = _as_mapping(config.get("raw_tools") or {}, "raw_tools")
    trajectory = _as_mapping(config.get("trajectory", {}), "trajectory")
    source_catalog = _as_mapping(config.get("source_catalog") or {}, "source_catalog")
    evidence = _as_mapping(config.get("evidence") or {}, "evidence")

    wiki_root = _resolve(root, str(wiki.get("root") or "wiki"))
    card_value = wiki.get("card_index", "cards")
    rule_value = wiki.get("rule_index", "rules/index.jsonl")
    uploaded_dir = _resolve(
        root, str(trajectory.get("uploaded_dir") or "raw/sessions/uploaded")
    )
    delta_value = trajectory.get("delta_dir")
    delta_dir = (
        _resolve(root, str(delta_value))
        if delta_value
        else uploaded_dir.parent / "deltas"
    )
    return RepositoryPaths(
        repo_root=root,
        config_path=config_path,
        wiki_root=wiki_root,
        static_index_path=(
            wiki_root / str(wiki.get("static_index") or "guide/static_index.jsonl")
        ).resolve(),
        dynamic_index_path=(
            wiki_root / str(wiki.get("dynamic_index") or "guide/dynamic_index.jsonl")
        ).resolve(),
        card_index_path=(wiki_root / str(card_value)).resolve() if card_value else None,
        rule_index_path=(wiki_root / str(rule_value)).resolve() if rule_value else None,
        nav_index_path=(wiki_root / str(wiki.get("nav_index") or "index.md")).resolve(),
        raw_root=_resolve(root, str(raw_cfg.get("root") or "raw")),
        raw_max_lines=max(1, int(raw_cfg.get("max_lines", 400))),
        raw_max_bytes=max(1024, int(raw_cfg.get("max_bytes", 131072))),
        trajectory_delta_dir=delta_dir.resolve(),
        source_records_dir=_resolve(
            root, str(source_catalog.get("records_dir") or "data/source-catalog")
        ),
        evidence_cas_dir=_resolve(
            root, str(evidence.get("cas_dir") or "artifacts/cas")
        ),
        evidence_review_dir=_resolve(
            root, str(evidence.get("review_dir") or "raw/sessions/evidence-review")
        ),
    )


def discover_repo_root(start: Path | str | None = None) -> Path:
    root = configured_resource_root(start)
    if not (root / "wiki").is_dir():
        raise FileNotFoundError(
            f"Knowledge resource root {root} has no wiki/ directory"
        )
    return root
