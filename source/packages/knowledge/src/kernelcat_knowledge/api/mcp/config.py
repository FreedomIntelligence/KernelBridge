"""MCP paths and deterministic lexical-search configuration."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from kernelcat_knowledge.resources.configuration import configured_resource_root


def resource_root() -> Path:
    return configured_resource_root()


class ConfigError(RuntimeError):
    """Raised when repository configuration is invalid."""


@dataclass(frozen=True)
class Config:
    wiki_root: Path
    static_index_path: Path
    dynamic_index_path: Path
    nav_index_path: Path
    search_mode: str
    trajectory_uploaded_dir: Path
    trajectory_delta_dir: Path
    card_index_path: Path | None = None
    rule_index_path: Path | None = None
    aliases_path: Path | None = None
    raw_root: Path | None = None
    raw_max_lines: int = 400
    raw_max_bytes: int = 131072


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"config.yaml field {field!r} must be a mapping")
    return value


def _require(node: dict, key: str, where: str) -> Any:
    if key not in node:
        prefix = f"{where}." if where else ""
        raise ConfigError(f"config.yaml missing field: {prefix}{key}")
    return node[key]


def _resolve_path(base: Path, raw: str | Path) -> Path:
    path = Path(raw)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config.yaml not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"config.yaml parse failed: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml top-level value must be a mapping")
    return raw


def _search_config(raw: dict[str, Any]) -> dict[str, Any]:
    search = _mapping(_require(raw, "search", ""), "search")
    mode = _require(search, "mode", "search")
    if mode != "lexical":
        raise ConfigError(f"unsupported search.mode {mode!r}; expected 'lexical'")
    retired_fields = sorted(set(raw) & {"local", "openai_api", "claude_agent"})
    if retired_fields or "weights" in search:
        raise ConfigError("retired provider configuration; remove provider sections and search.weights")
    return search


def load_config(path: Path | None = None) -> Config:
    path = path if path is not None else resource_root() / "config.yaml"
    raw = _read_config(path)
    search = _search_config(raw)
    wiki = _mapping(_require(raw, "wiki", ""), "wiki")
    trajectory = _mapping(_require(raw, "trajectory", ""), "trajectory")
    raw_cfg = _mapping(raw.get("raw_tools", {}), "raw_tools")

    base = path.parent.resolve()
    wiki_root = _resolve_path(base, _require(wiki, "root", "wiki"))
    card_value = wiki.get("card_index", "cards")
    rule_value = wiki.get("rule_index", "rules/index.jsonl")
    aliases_value = search.get("aliases_path", "data/aliases.yaml")
    raw_root_value = raw_cfg.get("root", "raw")
    uploaded_dir = _resolve_path(base, _require(trajectory, "uploaded_dir", "trajectory"))
    delta_value = trajectory.get("delta_dir")
    delta_dir = _resolve_path(base, delta_value) if delta_value else uploaded_dir.parent / "deltas"

    return Config(
        wiki_root=wiki_root,
        static_index_path=wiki_root / str(_require(wiki, "static_index", "wiki")),
        dynamic_index_path=wiki_root / str(_require(wiki, "dynamic_index", "wiki")),
        nav_index_path=wiki_root / str(_require(wiki, "nav_index", "wiki")),
        search_mode=search["mode"],
        trajectory_uploaded_dir=uploaded_dir,
        trajectory_delta_dir=delta_dir,
        card_index_path=(wiki_root / str(card_value)).resolve() if card_value else None,
        rule_index_path=(wiki_root / str(rule_value)).resolve() if rule_value else None,
        aliases_path=_resolve_path(base, str(aliases_value)) if aliases_value else None,
        raw_root=_resolve_path(base, str(raw_root_value)) if raw_root_value else None,
        raw_max_lines=max(1, int(raw_cfg.get("max_lines", 400))),
        raw_max_bytes=max(1024, int(raw_cfg.get("max_bytes", 131072))),
    )


class _LazyConfig:
    def __getattr__(self, name: str) -> Any:
        return getattr(load_config(), name)


CONFIG = _LazyConfig()
