#!/usr/bin/env python3
"""Produce fail-closed route and source-diff identities for profiler diagnosis.

The producer never guesses a runtime route.  Source attribution is derived from
content-addressed base/candidate trees; route identity is verified only from an
explicit ``KERNELCAT_ROUTE_EVIDENCE {json}`` marker captured in the candidate
Event log.  Missing markers remain visible evidence and cap diagnosis at L1.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

SOURCE_EVIDENCE_FILENAME = "kernelcat_evidence.json"
SOURCE_EVIDENCE_SCHEMA = "kernelcat.source-evidence/v1"
ROUTE_OBSERVATION_SCHEMA = "kernelcat.route-observation/v1"
ROUTE_MARKER = "KERNELCAT_ROUTE_EVIDENCE"

_IGNORED_PARTS = {
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__",
    "build", "dist",
}
_TEXT_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".py", ".cmake", ".txt", ".json", ".yaml", ".yml",
}
_CONTROL_NAMES = {"if", "for", "while", "switch", "catch", "return", "sizeof"}
_SYMBOL_RE = re.compile(
    r"(?:^|\s)(?:class|struct|enum)\s+([A-Za-z_]\w*)"
    r"|(?:^|\s)([A-Za-z_~][\w:~]*)\s*\([^;{}]*\)\s*(?:const\s*)?(?:\{|$)"
)
_ROUTE_RE = re.compile(r"KERNELCAT_ROUTE_EVIDENCE\s+(\{.*\})")
_BUILD_RE = re.compile(r"KERNELCAT_BUILD_IDENTITY\s+(\{.*\})")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_text(path: Path) -> list[str] | None:
    if path.name == "CMakeLists.txt" or path.suffix.lower() in _TEXT_SUFFIXES:
        try:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
    return None


def _inventory(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        lowered = {part.lower() for part in relative.parts}
        if (
            lowered & _IGNORED_PARTS
            or not path.is_file()
            or relative.as_posix() == SOURCE_EVIDENCE_FILENAME
        ):
            continue
        if _safe_text(path) is not None:
            result[relative.as_posix()] = path
    return result


def _nearest_symbol(lines: list[str], index: int) -> str | None:
    for offset in range(min(index, len(lines) - 1), max(-1, index - 80), -1):
        line = lines[offset].strip()
        match = _SYMBOL_RE.search(line)
        if not match:
            continue
        name = match.group(1) or match.group(2)
        if name and name.split("::")[-1] not in _CONTROL_NAMES:
            return name
    return None


def _source_manifest(candidate_source: Path) -> tuple[dict[str, Any], list[str]]:
    path = candidate_source / SOURCE_EVIDENCE_FILENAME
    if not path.is_file():
        return {}, ["source_evidence_manifest_missing"]
    errors: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"invalid_source_evidence_manifest:{exc}"]
    if not isinstance(payload, dict):
        return {}, ["source_evidence_manifest_must_be_object"]
    if payload.get("schema_version") != SOURCE_EVIDENCE_SCHEMA:
        errors.append("source_evidence_schema_mismatch")
    for field in ("policy_id", "expected_route", "capability_predicate"):
        if payload.get(field) in (None, ""):
            errors.append(f"source_evidence_missing_{field}")
    bottlenecks = payload.get("bottleneck_ids", [])
    if (
        not isinstance(bottlenecks, list)
        or not bottlenecks
        or any(not str(item).strip() for item in bottlenecks)
    ):
        errors.append("source_evidence_bottleneck_ids_invalid")
    return payload, errors


def build_code_attribution(
    base_source: Path,
    candidate_source: Path,
    *,
    source_hashes: Mapping[str, str],
    action_cell: str,
    artifact_ref: str,
    max_regions: int = 48,
    max_symbols: int = 32,
) -> dict[str, Any]:
    """Compute changed files/regions/symbols and bind them to source SHA-256s."""
    base_source = Path(base_source).resolve()
    candidate_source = Path(candidate_source).resolve()
    base_files = _inventory(base_source)
    candidate_files = _inventory(candidate_source)
    manifest, manifest_errors = _source_manifest(candidate_source)
    regions: list[str] = []
    symbols: list[str] = []
    changed_files: list[str] = []

    for relative in sorted(set(base_files) | set(candidate_files)):
        before = _safe_text(base_files[relative]) if relative in base_files else []
        after = _safe_text(candidate_files[relative]) if relative in candidate_files else []
        before = before or []
        after = after or []
        if before == after:
            continue
        changed_files.append(relative)
        matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if len(regions) < max_regions:
                if j1 != j2:
                    regions.append(f"{relative}:L{j1 + 1}-L{max(j1 + 1, j2)}")
                else:
                    regions.append(f"{relative}:deleted-base-L{i1 + 1}-L{max(i1 + 1, i2)}")
            symbol = _nearest_symbol(after, j1) if after else None
            if symbol and symbol not in symbols and len(symbols) < max_symbols:
                symbols.append(symbol)

    policy_id = str(manifest.get("policy_id") or action_cell).strip()
    policy_mismatch = bool(policy_id and action_cell and policy_id != action_cell)
    hashes = {
        "base": str(source_hashes.get("base") or ""),
        "candidate": str(source_hashes.get("mt") or source_hashes.get("candidate") or ""),
    }
    valid_hashes = all(re.fullmatch(r"[0-9a-fA-F]{64}", value or "") for value in hashes.values())
    verified = bool(changed_files and (regions or symbols) and valid_hashes and not manifest_errors and not policy_mismatch)
    status = "verified" if verified else (
        "mismatch" if policy_mismatch else "unchanged" if not changed_files else "asserted_unverified"
    )
    return {
        "schema_version": "kernelcat.code-attribution/v1",
        "status": status,
        "verified": verified,
        "policy_id": policy_id or None,
        "action_cell": action_cell,
        "bottleneck_ids": sorted({str(item) for item in manifest.get("bottleneck_ids", [])}),
        "changed_files": changed_files,
        "changed_symbols": symbols,
        "changed_regions": regions,
        "source_tree_sha256": hashes,
        "source_evidence_manifest": SOURCE_EVIDENCE_FILENAME if manifest else None,
        "manifest_errors": manifest_errors,
        "artifact_refs": [artifact_ref],
    }


def _log_evidence(
    log_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    observations: list[dict[str, Any]] = []
    builds: list[dict[str, Any]] = []
    errors: list[str] = []
    if not log_path.is_file():
        return observations, builds, ["candidate_event_log_missing"]
    for line_number, line in enumerate(
        log_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        for kind, pattern, target in (
            ("route", _ROUTE_RE, observations),
            ("build", _BUILD_RE, builds),
        ):
            match = pattern.search(line)
            if not match:
                continue
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError as exc:
                errors.append(f"{kind}_marker_json_error:L{line_number}:{exc.msg}")
                continue
            if not isinstance(payload, dict):
                errors.append(f"{kind}_marker_not_object:L{line_number}")
                continue
            payload = dict(payload)
            payload["_line"] = line_number
            target.append(payload)
    return observations, builds, errors


def build_route_identity(
    candidate_event_log: Path,
    *,
    run_id: str,
    case_id: int,
    candidate_arm: str,
    action_cell: str,
    candidate_source_sha256: str,
    candidate_source: Path,
    event_log_ref: str,
    artifact_ref: str,
) -> dict[str, Any]:
    """Verify one runtime route observation against run/source identity."""
    manifest, manifest_errors = _source_manifest(Path(candidate_source))
    observations, builds, parse_errors = _log_evidence(Path(candidate_event_log))
    errors = [*manifest_errors, *parse_errors]
    expected_route = str(manifest.get("expected_route") or "").strip() or None

    if not observations:
        return {
            "schema_version": "kernelcat.route-identity/v1",
            "status": "missing",
            "verified": False,
            "route_hit": False,
            "expected_route": expected_route,
            "observed_route": None,
            "tiling_key": None,
            "capability_predicate": manifest.get("capability_predicate"),
            "action_cell": action_cell,
            "build_identity": candidate_source_sha256,
            "build_observations": len(builds),
            "observations": 0,
            "errors": errors or ["route_marker_missing"],
            "artifact_refs": [event_log_ref, artifact_ref],
        }

    comparable_fields = (
        "observed_route", "route", "tiling_key", "route_key",
        "capability_predicate", "action_cell", "build_identity", "route_hit",
    )
    canonical = {
        _canonical({key: item.get(key) for key in comparable_fields if key in item})
        for item in observations
    }
    if len(canonical) != 1:
        errors.append("conflicting_route_observations")
    observation = observations[0]
    if observation.get("schema_version") != ROUTE_OBSERVATION_SCHEMA:
        errors.append("route_observation_schema_mismatch")
    for field, expected in (
        ("run_id", run_id), ("case_id", case_id), ("arm", candidate_arm),
        ("action_cell", action_cell),
    ):
        if str(observation.get(field)) != str(expected):
            errors.append(f"route_observation_{field}_mismatch")

    build_canonical = {
        _canonical({key: item.get(key) for key in (
            "schema_version", "run_id", "case_id", "arm", "action_cell",
            "source_tree_sha256",
        )})
        for item in builds
    }
    if not builds:
        errors.append("build_identity_marker_missing")
        build = {}
    else:
        if len(build_canonical) != 1:
            errors.append("conflicting_build_identity_observations")
        build = builds[0]
        if build.get("schema_version") != "kernelcat.route-context/v1":
            errors.append("build_identity_schema_mismatch")
        for field, expected in (
            ("run_id", run_id), ("case_id", case_id), ("arm", candidate_arm),
            ("action_cell", action_cell),
        ):
            if str(build.get(field)) != str(expected):
                errors.append(f"build_identity_{field}_mismatch")
        if str(build.get("source_tree_sha256") or "") != candidate_source_sha256:
            errors.append("build_identity_source_sha_mismatch")

    claimed_build = observation.get("build_identity")
    if isinstance(claimed_build, Mapping):
        claimed_sha = str(
            claimed_build.get("candidate") or claimed_build.get("mt")
            or claimed_build.get("source_tree_sha256") or ""
        )
    else:
        claimed_sha = str(claimed_build or "")
    if claimed_sha and claimed_sha != candidate_source_sha256:
        errors.append("route_observation_build_identity_mismatch")
    observed_route = str(observation.get("observed_route") or observation.get("route") or "").strip()
    if expected_route and observed_route and re.sub(r"\W+", "", expected_route).lower() != re.sub(r"\W+", "", observed_route).lower():
        errors.append("expected_observed_route_mismatch")
    tiling_key = observation.get("tiling_key", observation.get("route_key"))
    capability = observation.get("capability_predicate") or manifest.get("capability_predicate")
    for field, value in (
        ("route_hit", observation.get("route_hit") is True),
        ("observed_route", observed_route),
        ("tiling_key", tiling_key),
        ("capability_predicate", capability),
    ):
        if value in (False, None, ""):
            errors.append(f"route_observation_missing_{field}")

    verified = not errors
    return {
        "schema_version": "kernelcat.route-identity/v1",
        "status": "verified" if verified else "mismatch",
        "verified": verified,
        "route_hit": observation.get("route_hit") is True,
        "expected_route": expected_route,
        "observed_route": observed_route or None,
        "tiling_key": tiling_key,
        "capability_predicate": capability,
        "action_cell": action_cell,
        "build_identity": candidate_source_sha256,
        "build_observations": len(builds),
        "build_marker_lines": [int(item.get("_line") or 0) for item in builds],
        "observations": len(observations),
        "marker_lines": [int(item.get("_line") or 0) for item in observations],
        "errors": errors,
        "artifact_refs": [event_log_ref, artifact_ref],
    }


def captured_metric_groups(pair_dir: Path) -> list[str]:
    groups: set[str] = set()
    for path in Path(pair_dir).glob("pmu_*/**/pipeprof.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        group = str(payload.get("metric_group") or "").strip()
        if group:
            groups.add(group)
    return sorted(groups)
