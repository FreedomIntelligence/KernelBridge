#!/usr/bin/env python3
"""Fail-closed A/B gate for token-aware profiler field adoption.

Input records are dictionaries with ``op``, ``case_id``, binary ``label``,
numeric ``timing`` fields, and numeric ``profile`` fields.  The evaluator uses
nested, operator-grouped cross validation so cases from one operator never
appear in both train and test data.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np


SCHEMA_VERSION = "profiler-adoption/v1"
PROFILE_PROVENANCE_FIELDS = (
    "soc",
    "cann_version",
    "compiler_version",
    "dtype",
    "shape_domain",
    "device",
    "profiler_level",
)
REQUIRED_PROFILE_PROVENANCE = (
    "soc",
    "cann_version",
    "compiler_version",
    "dtype",
    "shape_domain",
)
ADOPTION_PROVENANCE_FIELDS = REQUIRED_PROFILE_PROVENANCE
POST_OUTCOME_TIMING = re.compile(
    r"(^|_)(candidate|mt|arm|speedup|accepted|label|outcome)(_|$)", re.IGNORECASE
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value.endswith("%"):
            value = value[:-1].strip()
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _field_value(record: dict[str, Any], field: str) -> float | None:
    section, name = field.split(".", 1)
    return _number(record.get(section, {}).get(name))


def _variable_fields(records: list[dict[str, Any]], section: str) -> list[str]:
    names = sorted({name for row in records for name in row.get(section, {})})
    result = []
    for name in names:
        values = {_number(row.get(section, {}).get(name)) for row in records}
        values.discard(None)
        if len(values) >= 2:
            result.append(name)
    return result


def _context(record: dict[str, Any], profile_fields: Iterable[str]) -> str:
    timing = {
        key: value
        for key, value in sorted(record.get("timing", {}).items())
        if _number(value) is not None
    }
    profile = {
        key: record.get("profile", {}).get(key)
        for key in sorted(profile_fields)
        if _number(record.get("profile", {}).get(key)) is not None
    }
    return json.dumps(
        {"timing": timing, "profile": profile},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _resolve_token_counter(
    token_counter: Callable[[str], int] | None,
) -> tuple[Callable[[str], int], str, bool]:
    if token_counter is not None:
        return token_counter, "custom", True
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("o200k_base")
        return lambda text: len(encoding.encode(text)), "tiktoken:o200k_base", True
    except Exception:  # pragma: no cover - exercised only without optional tokenizer
        return (
            lambda text: max(1, (len(text.encode("utf-8")) + 3) // 4),
            "utf8-bytes/4",
            False,
        )


def _fit_ridge(records: list[dict[str, Any]], fields: list[str], alpha: float = 1.0):
    labels = np.asarray([int(bool(row["label"])) for row in records], dtype=float)
    if not fields:
        return {"fields": [], "prior": float(labels.mean()) if len(labels) else 0.0}

    medians: dict[str, float] = {}
    columns = []
    for field in fields:
        present = [_field_value(row, field) for row in records]
        numeric = [value for value in present if value is not None]
        median = float(np.median(numeric)) if numeric else 0.0
        medians[field] = median
        columns.append([median if value is None else value for value in present])
    matrix = np.asarray(columns, dtype=float).T
    means = matrix.mean(axis=0)
    scales = matrix.std(axis=0)
    scales[scales < 1e-12] = 1.0
    matrix = (matrix - means) / scales
    design = np.column_stack([np.ones(len(records)), matrix])
    penalty = np.eye(design.shape[1]) * alpha
    penalty[0, 0] = 0.0
    coef = np.linalg.pinv(design.T @ design + penalty) @ design.T @ labels
    return {
        "fields": fields,
        "medians": medians,
        "means": means,
        "scales": scales,
        "coef": coef,
    }


def _predict(model: dict[str, Any], records: list[dict[str, Any]]) -> list[int]:
    fields = model["fields"]
    if not fields:
        prediction = int(model["prior"] >= 0.5)
        return [prediction] * len(records)
    columns = []
    for field in fields:
        median = model["medians"][field]
        columns.append(
            [median if _field_value(row, field) is None else _field_value(row, field) for row in records]
        )
    matrix = np.asarray(columns, dtype=float).T
    matrix = (matrix - model["means"]) / model["scales"]
    design = np.column_stack([np.ones(len(records)), matrix])
    return [int(value >= 0.5) for value in design @ model["coef"]]


def _balanced_accuracy(labels: list[int], predictions: list[int]) -> float | None:
    recalls = []
    for label in (0, 1):
        positions = [index for index, value in enumerate(labels) if value == label]
        if positions:
            recalls.append(sum(predictions[index] == label for index in positions) / len(positions))
    return sum(recalls) / len(recalls) if recalls else None


def _rank_profile_fields(records: list[dict[str, Any]], fields: list[str]) -> list[str]:
    labels = np.asarray([int(bool(row["label"])) for row in records], dtype=int)
    ranked = []
    for field in fields:
        pairs = [
            (_number(row.get("profile", {}).get(field)), int(bool(row["label"])))
            for row in records
        ]
        pairs = [(value, label) for value, label in pairs if value is not None]
        zeros = [value for value, label in pairs if label == 0]
        ones = [value for value, label in pairs if label == 1]
        if not zeros or not ones:
            score = 0.0
        else:
            spread = float(np.std([value for value, _ in pairs]))
            separation = abs(float(np.mean(ones)) - float(np.mean(zeros))) / max(spread, 1e-12)
            score = separation * math.sqrt(len(pairs) / max(len(labels), 1))
        ranked.append((score, field))
    return [field for _, field in sorted(ranked, key=lambda item: (-item[0], item[1]))]


def _choose_dynamic_fields(
    records: list[dict[str, Any]],
    timing_fields: list[str],
    profile_fields: list[str],
    token_counter: Callable[[str], int],
    max_fields: int = 8,
    token_penalty: float = 0.02,
) -> list[str]:
    groups = sorted({str(row["op"]) for row in records})
    max_k = min(max_fields, len(profile_fields))
    if max_k == 0:
        return []
    if len(groups) < 3:
        return _rank_profile_fields(records, profile_fields)[:1]

    candidates = []
    # A "dynamic profiler" policy that selects zero profiler fields is just the
    # timing-only baseline.  Allowing k=0 here previously let that baseline win
    # the token penalty and then be misreported as a profiler adoption.
    for k in range(1, max_k + 1):
        labels: list[int] = []
        predictions: list[int] = []
        tokens = 0
        all_tokens = 0
        valid = True
        for held_out in groups:
            train = [row for row in records if str(row["op"]) != held_out]
            test = [row for row in records if str(row["op"]) == held_out]
            if len({int(bool(row["label"])) for row in train}) < 2:
                valid = False
                break
            selected = _rank_profile_fields(train, profile_fields)[:k]
            fields = [f"timing.{name}" for name in timing_fields]
            fields += [f"profile.{name}" for name in selected]
            model = _fit_ridge(train, fields)
            labels.extend(int(bool(row["label"])) for row in test)
            predictions.extend(_predict(model, test))
            tokens += sum(token_counter(_context(row, selected)) for row in test)
            all_tokens += sum(token_counter(_context(row, profile_fields)) for row in test)
        quality = _balanced_accuracy(labels, predictions) if valid else None
        if quality is not None:
            objective = quality - token_penalty * (tokens / max(all_tokens, 1))
            candidates.append((objective, -k, k))
    best_k = max(candidates)[2] if candidates else 1
    return _rank_profile_fields(records, profile_fields)[:best_k]


def _bootstrap_lower(values: list[float], samples: int, seed: int) -> float | None:
    if not values:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(max(samples, 1)):
        draw = [values[rng.randrange(len(values))] for _ in values]
        means.append(sum(draw) / len(draw))
    means.sort()
    return means[max(0, int(0.025 * (len(means) - 1)))]


def _empty_strategy(records, profile_fields, counter):
    tokens = sum(counter(_context(row, profile_fields)) for row in records)
    return {
        "balanced_accuracy": None,
        "total_tokens": tokens,
        "mean_tokens_per_record": tokens / len(records) if records else 0.0,
        "fold_fields": {},
    }


def evaluate_adoption(
    records: list[dict[str, Any]],
    legacy_fields: list[str],
    token_counter: Callable[[str], int] | None = None,
    *,
    min_groups: int = 5,
    min_records: int = 40,
    min_class_records: int = 10,
    max_dynamic_fields: int = 8,
    bootstrap_samples: int = 2000,
    seed: int = 20260824,
) -> dict[str, Any]:
    counter, counter_name, exact_tokens = _resolve_token_counter(token_counter)
    groups = sorted({str(row.get("op", "")) for row in records})
    labels = [int(bool(row.get("label"))) for row in records]
    label_counts = Counter(labels)
    timing_fields = _variable_fields(records, "timing")
    profile_fields = _variable_fields(records, "profile")
    action_cells = sorted(
        {str(row.get("action_cell")) for row in records if row.get("action_cell")}
    )
    missing_action_cells = sum(not row.get("action_cell") for row in records)
    legacy = [field.removeprefix("profile.") for field in legacy_fields]
    legacy = sorted(set(legacy) & set(profile_fields))

    provenance_cells: set[tuple[str, ...]] = set()
    missing_provenance = 0
    record_key_counts: Counter[tuple[str, ...]] = Counter()
    missing_record_keys = 0
    for row in records:
        provenance = row.get("provenance") or {}
        cell = tuple(str(provenance.get(field) or "").strip() for field in ADOPTION_PROVENANCE_FIELDS)
        if any(not value for value in cell):
            missing_provenance += 1
        else:
            provenance_cells.add(cell)
        record_key = tuple(
            str(row.get(field) or "").strip()
            for field in ("run_id", "op", "arm", "case_id")
        )
        if any(not value for value in record_key):
            missing_record_keys += 1
        else:
            record_key_counts[record_key] += 1
    duplicate_record_keys = sorted(
        key for key, count in record_key_counts.items() if count > 1
    )
    provenance_payload = [
        dict(zip(ADOPTION_PROVENANCE_FIELDS, cell)) for cell in sorted(provenance_cells)
    ]

    reasons = []
    if len(groups) < min_groups:
        reasons.append(f"need at least {min_groups} operator groups, found {len(groups)}")
    if len(records) < min_records:
        reasons.append(f"need at least {min_records} labeled records, found {len(records)}")
    if min(label_counts.get(0, 0), label_counts.get(1, 0)) < min_class_records:
        reasons.append(
            f"need at least {min_class_records} records in each label class, found {dict(label_counts)}"
        )
    if len(profile_fields) < 2:
        reasons.append(f"need at least 2 variable profile fields, found {len(profile_fields)}")
    leaky_timing = sorted(
        {
            name
            for row in records
            for name in row.get("timing", {})
            if POST_OUTCOME_TIMING.search(str(name))
        }
    )
    if leaky_timing:
        reasons.append(f"post-outcome timing fields are forbidden: {leaky_timing}")
    if missing_action_cells or len(action_cells) != 1:
        reasons.append(
            "need exactly one action_cell with no missing values; "
            f"found cells={action_cells}, missing={missing_action_cells}"
        )
    if missing_provenance or len(provenance_cells) != 1:
        reasons.append(
            "need exactly one complete (soc, cann_version, compiler_version, dtype, "
            "shape_domain) provenance cell; "
            f"found cells={provenance_payload}, missing={missing_provenance}"
        )
    if missing_record_keys or duplicate_record_keys:
        reasons.append(
            "need unique complete (run_id, op, arm, case_id) record keys; "
            f"missing={missing_record_keys}, duplicates={duplicate_record_keys}"
        )
    if not exact_tokens:
        reasons.append("exact token counter unavailable")

    strategy_profiles = {
        "timing_only": [],
        "legacy": legacy,
        "all_fields": profile_fields,
        "dynamic": [],
    }
    if reasons:
        strategies = {
            name: _empty_strategy(records, fields, counter)
            for name, fields in strategy_profiles.items()
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "dataset": {
                "records": len(records),
                "groups": len(groups),
                "label_counts": dict(sorted(label_counts.items())),
                "timing_fields": timing_fields,
                "profile_fields": profile_fields,
                "action_cells": action_cells,
                "provenance_cells": provenance_payload,
            },
            "protocol": {
                "split": "leave-one-operator-out with nested field selection",
                "scope": "pre-action counters for one taxonomy action_cell",
                "evidence_level": "offline_predictive_proxy",
                "token_scope": "serialized feedback context only, not optimizer output tokens",
                "token_counter": counter_name,
                "exact_tokens": exact_tokens,
            },
            "strategies": strategies,
            "comparisons": {},
            "decision": {"adopt": False, "selected_fields": [], "reasons": reasons},
        }

    predictions: dict[str, dict[int, int]] = {name: {} for name in strategy_profiles}
    group_stats: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    fold_fields: dict[str, dict[str, list[str]]] = {name: {} for name in strategy_profiles}
    token_totals = Counter()

    for held_out in groups:
        train = [row for row in records if str(row["op"]) != held_out]
        indexed_test = [(index, row) for index, row in enumerate(records) if str(row["op"]) == held_out]
        test = [row for _, row in indexed_test]
        dynamic = _choose_dynamic_fields(
            train,
            timing_fields,
            profile_fields,
            counter,
            max_fields=max_dynamic_fields,
        )
        fold_profiles = {
            "timing_only": [],
            "legacy": legacy,
            "all_fields": profile_fields,
            "dynamic": dynamic,
        }
        for strategy, selected in fold_profiles.items():
            fields = [f"timing.{name}" for name in timing_fields]
            fields += [f"profile.{name}" for name in selected]
            model = _fit_ridge(train, fields)
            fold_predictions = _predict(model, test)
            fold_labels = [int(bool(row["label"])) for row in test]
            fold_tokens = sum(counter(_context(row, selected)) for row in test)
            token_totals[strategy] += fold_tokens
            fold_fields[strategy][held_out] = selected
            group_stats[held_out][strategy] = {
                "accuracy": sum(a == b for a, b in zip(fold_labels, fold_predictions)) / len(test),
                "tokens": float(fold_tokens),
            }
            for (index, _), prediction in zip(indexed_test, fold_predictions):
                predictions[strategy][index] = prediction

    strategies = {}
    for strategy in strategy_profiles:
        ordered_predictions = [predictions[strategy][index] for index in range(len(records))]
        strategies[strategy] = {
            "balanced_accuracy": _balanced_accuracy(labels, ordered_predictions),
            "total_tokens": int(token_totals[strategy]),
            "mean_tokens_per_record": token_totals[strategy] / len(records),
            "fold_fields": fold_fields[strategy],
        }

    dynamic_quality = strategies["dynamic"]["balanced_accuracy"]
    timing_quality = strategies["timing_only"]["balanced_accuracy"]
    legacy_quality = strategies["legacy"]["balanced_accuracy"]
    all_quality = strategies["all_fields"]["balanced_accuracy"]
    dynamic_tokens = strategies["dynamic"]["total_tokens"]
    legacy_tokens = strategies["legacy"]["total_tokens"]

    quality_deltas = [
        group_stats[group]["dynamic"]["accuracy"] - group_stats[group]["legacy"]["accuracy"]
        for group in groups
    ]
    all_deltas = [
        group_stats[group]["dynamic"]["accuracy"] - group_stats[group]["all_fields"]["accuracy"]
        for group in groups
    ]
    timing_deltas = [
        group_stats[group]["dynamic"]["accuracy"]
        - group_stats[group]["timing_only"]["accuracy"]
        for group in groups
    ]
    token_savings = [
        (
            group_stats[group]["legacy"]["tokens"] - group_stats[group]["dynamic"]["tokens"]
        )
        / max(group_stats[group]["legacy"]["tokens"], 1.0)
        for group in groups
    ]
    quality_lcb = _bootstrap_lower(quality_deltas, bootstrap_samples, seed)
    all_lcb = _bootstrap_lower(all_deltas, bootstrap_samples, seed + 1)
    token_lcb = _bootstrap_lower(token_savings, bootstrap_samples, seed + 2)
    timing_lcb = _bootstrap_lower(timing_deltas, bootstrap_samples, seed + 3)

    selected_fields = _choose_dynamic_fields(
        records,
        timing_fields,
        profile_fields,
        counter,
        max_fields=max_dynamic_fields,
    )

    checks = {
        "selected_profiler_fields_nonempty": bool(selected_fields),
        "quality_strictly_better_than_timing_only": dynamic_quality > timing_quality,
        "quality_strictly_better_than_legacy": dynamic_quality > legacy_quality,
        "tokens_strictly_lower_than_legacy": dynamic_tokens < legacy_tokens,
        "noninferior_to_all_fields_1pp": dynamic_quality >= all_quality - 0.01,
        "quality_gain_95pct_lcb_positive": quality_lcb is not None and quality_lcb > 0.0,
        "token_saving_95pct_lcb_positive": token_lcb is not None and token_lcb > 0.0,
        "all_fields_gap_95pct_lcb_above_minus_1pp": all_lcb is not None and all_lcb >= -0.01,
        "quality_gain_vs_timing_only_95pct_lcb_positive": (
            timing_lcb is not None and timing_lcb > 0.0
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    frequencies = Counter(
        field for selected in fold_fields["dynamic"].values() for field in selected
    )
    comparisons = {
        "dynamic_minus_legacy_quality": dynamic_quality - legacy_quality,
        "dynamic_minus_timing_only_quality": dynamic_quality - timing_quality,
        "dynamic_vs_legacy_token_reduction": 1.0 - dynamic_tokens / max(legacy_tokens, 1),
        "dynamic_minus_all_fields_quality": dynamic_quality - all_quality,
        "quality_gain_95pct_lcb": quality_lcb,
        "token_saving_95pct_lcb": token_lcb,
        "all_fields_gap_95pct_lcb": all_lcb,
        "quality_gain_vs_timing_only_95pct_lcb": timing_lcb,
        "checks": checks,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": {
            "records": len(records),
            "groups": len(groups),
            "label_counts": dict(sorted(label_counts.items())),
            "timing_fields": timing_fields,
            "profile_fields": profile_fields,
            "action_cells": action_cells,
            "provenance_cells": provenance_payload,
        },
        "protocol": {
            "split": "leave-one-operator-out with nested field selection",
            "scope": "pre-action counters for one taxonomy action_cell",
            "evidence_level": "offline_predictive_proxy",
            "token_scope": "serialized feedback context only, not optimizer output tokens",
            "model": "ridge classifier",
            "token_counter": counter_name,
            "exact_tokens": exact_tokens,
            "bootstrap_samples": bootstrap_samples,
        },
        "strategies": strategies,
        "comparisons": comparisons,
        "selection_frequency": dict(sorted(frequencies.items())),
        "decision": {
            "adopt": not failed,
            "selected_fields": selected_fields if not failed else [],
            "reasons": failed,
        },
    }


def _case_index(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    match = re.search(r"(\d+)$", str(value or ""))
    return int(match.group(1)) if match else None


def _canonical_metric(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.]+", "_", value).strip("_")


def build_timing_ab_analysis(
    timing_reports: list[dict[str, Any]],
    *,
    base_arm: str = "base",
    action_cell: str,
    stat: str = "p50",
    min_speedup: float = 1.02,
    bootstrap_samples: int = 1000,
    seed: int = 20260824,
) -> dict[str, Any]:
    """Build latency A/B input solely from same-run profiler-off event samples.

    The builder is deliberately fail-closed: all accepted reports must belong
    to one operator, one run and one five-dimensional hardware/software cell.
    A conflicting duplicate capture invalidates the whole analysis.
    """
    indexed: dict[tuple[str, int], float] = {}
    sample_counts: dict[tuple[str, int], int] = {}
    sample_values: dict[tuple[str, int], list[float]] = {}
    stability: dict[tuple[str, int], dict[str, Any]] = {}
    contexts: set[tuple[str, str, tuple[str, ...], tuple[Any, ...]]] = set()
    metadata_by_context: dict[
        tuple[str, str, tuple[str, ...], tuple[Any, ...]], dict[str, Any]
    ] = {}
    conflict = False

    for report in timing_reports:
        if (
            report.get("schema_version") != "profiler-off-timing/v1"
            or report.get("profiler_enabled") is not False
            or report.get("timer") != "npu_event"
            or report.get("unit") != "us"
        ):
            continue
        metadata = report.get("metadata") or {}
        operator = str(metadata.get("operator") or "").strip()
        run_id = str(metadata.get("run_id") or "").strip()
        arm = str(metadata.get("arm") or "").strip()
        case_id = _case_index(metadata.get("case_id"))
        provenance = tuple(
            str(metadata.get(field) or "").strip()
            for field in ADOPTION_PROVENANCE_FIELDS
        )
        samples = [_number(value) for value in (report.get("samples_us") or [])]
        samples = [value for value in samples if value is not None and value > 0]
        quantile = {"p50": 0.50, "p95": 0.95, "p99": 0.99}.get(stat)
        duration = _percentile(samples, quantile) if quantile is not None else None
        required_samples = {"p50": 30, "p95": 100, "p99": 300}.get(stat, 10**9)
        if (
            not operator
            or not run_id
            or not arm
            or case_id is None
            or any(not value for value in provenance)
            or duration is None
            or duration <= 0
            or len(samples) < required_samples
        ):
            continue
        protocol = (
            str(metadata.get("device") or ""),
            metadata.get("warmup"),
            int(metadata.get("inner_repeat") or 1),
            str(metadata.get("timing_scope") or "single_call"),
            metadata.get("cache_clean_each_sample"),
        )
        context = (operator, run_id, provenance, protocol)
        contexts.add(context)
        metadata_by_context[context] = metadata
        key = (arm, case_id)
        if key in indexed and not math.isclose(indexed[key], duration, rel_tol=0.0, abs_tol=1e-12):
            conflict = True
        indexed[key] = duration
        sample_counts[key] = len(samples)
        sample_values[key] = samples
        p50 = _percentile(samples, 0.50)
        p95 = _percentile(samples, 0.95)
        mad = _percentile([abs(value - p50) for value in samples], 0.50)
        stability[key] = {
            "p95_p50_ratio": p95 / p50 if p50 else None,
            "mad_p50_ratio": mad / p50 if p50 else None,
            "stable": bool(p50 and p95 / p50 <= 1.20 and mad / p50 <= 0.05),
        }

    if conflict or len(contexts) != 1 or not action_cell:
        return {}
    context = next(iter(contexts))
    operator, run_id, provenance, protocol = context
    base_cases = {
        case_id: value for (arm, case_id), value in indexed.items() if arm == base_arm
    }
    arms = sorted({arm for arm, _ in indexed if arm != base_arm})
    diff: dict[str, Any] = {}
    for arm in arms:
        candidate_cases = {
            case_id: value for (row_arm, case_id), value in indexed.items() if row_arm == arm
        }
        rows = []
        for case_id in sorted(set(base_cases) & set(candidate_cases)):
            base_key = (base_arm, case_id)
            candidate_key = (arm, case_id)
            rng = random.Random(seed + case_id)
            boot = []
            base_samples = sample_values[base_key]
            candidate_samples = sample_values[candidate_key]
            if len(base_samples) != len(candidate_samples):
                continue
            for _ in range(max(bootstrap_samples, 1)):
                base_draw = [base_samples[rng.randrange(len(base_samples))] for _ in base_samples]
                candidate_draw = [
                    candidate_samples[rng.randrange(len(candidate_samples))]
                    for _ in candidate_samples
                ]
                base_median = _percentile(base_draw, 0.50)
                candidate_median = _percentile(candidate_draw, 0.50)
                if candidate_median and candidate_median > 0:
                    boot.append(base_median / candidate_median)
            speedup_lcb = _percentile(boot, 0.025)
            speedup = base_cases[case_id] / candidate_cases[case_id]
            stable = stability[base_key]["stable"] and stability[candidate_key]["stable"]
            rows.append(
                {
                    "idx": case_id,
                    "base_us": base_cases[case_id],
                    "mt_us": candidate_cases[case_id],
                    "x": speedup,
                    "speedup_95pct_lcb": speedup_lcb,
                    "stable": stable,
                    "accepted": bool(
                        stable
                        and speedup >= min_speedup
                        and speedup_lcb is not None
                        and speedup_lcb > 1.0
                    ),
                    "base_stability": stability[base_key],
                    "candidate_stability": stability[candidate_key],
                }
            )
        if rows:
            diff[arm] = {"rows": rows}
    if not diff:
        return {}

    metadata = metadata_by_context[context]
    return {
        "schema_version": "profiler-timing-ab/v1",
        "op": operator,
        "run_id": run_id,
        "action_cell": action_cell,
        "base": base_arm,
        "timing_stat": stat,
        "acceptance": {
            "min_speedup": min_speedup,
            "speedup_95pct_lcb_strictly_above": 1.0,
            "max_p95_p50_ratio": 1.20,
            "max_mad_p50_ratio": 0.05,
            "min_samples": {"p50": 30, "p95": 100, "p99": 300}[stat],
        },
        "provenance": {
            field: value for field, value in zip(ADOPTION_PROVENANCE_FIELDS, provenance)
        },
        "capture_metadata": {
            **{
                field: metadata.get(field)
                for field in (
                    "device",
                    "warmup",
                    "inner_repeat",
                    "timing_scope",
                    "cache_clean_each_sample",
                )
                if metadata.get(field) is not None
            },
            "profiler_enabled": False,
            "timer": "npu_event",
            "sample_counts": {
                "%s:%s" % (arm, case_id): count
                for (arm, case_id), count in sorted(sample_counts.items())
            },
        },
        "diff": diff,
    }


def build_ab_profile_records(
    analysis: dict[str, Any],
    profile_reports: list[dict[str, Any]],
    *,
    stat: str = "p50",
) -> list[dict[str, Any]]:
    """Pair ab_sdk analysis rows with base/candidate ``pipeprof/v1`` deltas."""
    op = str(analysis.get("op") or "").lower()
    action_cell = analysis.get("action_cell")
    base_arm = str(analysis.get("base") or "base")
    expected_provenance = {
        field: (analysis.get("provenance") or {}).get(field)
        for field in ADOPTION_PROVENANCE_FIELDS
    }
    if any(not expected_provenance[field] for field in ADOPTION_PROVENANCE_FIELDS):
        return []
    indexed: dict[tuple[str, str, str, int], dict[str, float]] = defaultdict(dict)
    provenance: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    conflicts: set[tuple[str, str, str, int]] = set()
    report_run_ids: set[str] = set()
    for report in profile_reports:
        if (
            report.get("schema_version") != "pipeprof/v1"
            or report.get("capture_kind") != "pmu"
            or not report.get("available")
        ):
            continue
        metadata = report.get("metadata") or {}
        report_op = str(metadata.get("operator") or "").lower()
        arm = str(metadata.get("arm") or "")
        run_id = str(metadata.get("run_id") or "")
        case_id = _case_index(metadata.get("case_id"))
        if (
            not report_op
            or not arm
            or not run_id
            or case_id is None
            or any(not metadata.get(field) for field in REQUIRED_PROFILE_PROVENANCE)
        ):
            continue
        report_run_ids.add(run_id)
        report_key = (run_id, report_op, arm, case_id)
        current_provenance = {
            field: metadata.get(field)
            for field in PROFILE_PROVENANCE_FIELDS
            if metadata.get(field) is not None
        }
        previous = provenance.setdefault(report_key, current_provenance)
        if previous != current_provenance:
            conflicts.add(report_key)
        group = _canonical_metric(str(report.get("metric_group") or "unknown"))
        weighted = report.get("duration_weighted_metrics") or {}
        if weighted:
            metric_items = weighted.items()
            value_name = "duration_weighted_mean"
        elif report.get("kernel_count") == 1:
            metric_items = (report.get("aggregate_metrics") or {}).items()
            value_name = stat
        else:
            metric_items = ()
            value_name = stat
        for column, stats in metric_items:
            value = _number((stats or {}).get(value_name))
            if value is not None:
                key = f"{group}.{_canonical_metric(str(column))}"
                previous_value = indexed[report_key].get(key)
                if previous_value is not None and not math.isclose(
                    previous_value, value, rel_tol=0.0, abs_tol=1e-12
                ):
                    conflicts.add(report_key)
                else:
                    indexed[report_key][key] = value

    requested_run_id = str(analysis.get("run_id") or "")
    if requested_run_id:
        run_id = requested_run_id
    elif len(report_run_ids) == 1:
        run_id = next(iter(report_run_ids))
    else:
        return []

    records = []
    for arm, arm_result in sorted((analysis.get("diff") or {}).items()):
        for row in arm_result.get("rows") or []:
            # ``accepted=False`` conflates a stable non-win with an unstable,
            # statistically inconclusive capture.  Never turn the latter into
            # a negative training label for dynamic profiler-field adoption.
            if row.get("stable") is False:
                continue
            case_id = _case_index(row.get("idx"))
            base_us = _number(row.get("base_us"))
            mt_us = _number(row.get("mt_us"))
            if case_id is None or base_us is None or mt_us is None or mt_us <= 0:
                continue
            base_key = (run_id, op, base_arm, case_id)
            mt_key = (run_id, op, str(arm), case_id)
            if base_key in conflicts or mt_key in conflicts:
                continue
            base_profile = indexed.get(base_key, {})
            mt_profile = indexed.get(mt_key, {})
            if not base_profile or not mt_profile:
                continue
            if provenance.get(base_key) != provenance.get(mt_key):
                continue
            if any(
                provenance[base_key].get(field) != expected_provenance[field]
                for field in ADOPTION_PROVENANCE_FIELDS
            ):
                continue
            profile: dict[str, float] = {}
            attribution: dict[str, float] = {}
            for field in sorted(set(base_profile) | set(mt_profile)):
                base_value = base_profile.get(field)
                mt_value = mt_profile.get(field)
                if base_value is not None:
                    profile[field] = base_value
                if mt_value is not None:
                    attribution[f"candidate.{field}"] = mt_value
                if base_value is not None and mt_value is not None and abs(base_value) > 1e-12:
                    attribution[f"delta.{field}"] = (mt_value - base_value) / abs(base_value)
            records.append(
                {
                    "op": op,
                    "arm": str(arm),
                    "run_id": run_id,
                    "provenance": dict(provenance[base_key]),
                    "action_cell": action_cell,
                    "case_id": str(case_id),
                    "label": int(
                        bool(row.get("accepted"))
                        if "accepted" in row
                        else mt_us < base_us
                    ),
                    "label_semantics": (
                        "same-run profiler-off acceptance gate"
                        if "accepted" in row
                        else "candidate latency lower than same-run base latency"
                    ),
                    "timing": {
                        "base_us": base_us,
                    },
                    "outcome": {"candidate_us": mt_us, "speedup": base_us / mt_us},
                    "profile": profile,
                    "attribution": attribution,
                }
            )
    return records


def build_rows910_records(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    records = []
    for key, entry in sorted(payload.items()):
        parts = key.split("|", 1)
        op = parts[0]
        split = parts[1] if len(parts) == 2 else "unknown"
        for index, triple in enumerate(entry.get("cases", []), start=1):
            if not isinstance(triple, list) or len(triple) < 3:
                continue
            baseline, hardware, candidate = map(_number, triple[:3])
            if None in (baseline, hardware, candidate) or candidate <= 0:
                continue
            records.append(
                {
                    "op": op,
                    "action_cell": None,
                    "split": split,
                    "case_id": f"{split}:{index}",
                    "job": entry.get("job"),
                    "label": int(candidate < baseline),
                    "label_semantics": "current submitted kernel faster than CANN baseline",
                    "timing": {
                        "baseline_us": baseline,
                        "hardware_us": hardware,
                    },
                    "outcome": {
                        "candidate_us": candidate,
                        "baseline_speedup": baseline / candidate,
                    },
                    "profile": {},
                }
            )
    return records


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * q
    low = int(position)
    high = min(low + 1, len(values) - 1)
    fraction = position - low
    return values[low] * (1 - fraction) + values[high] * fraction


def _compact_kernel_csv(
    rows: list[dict[str, str]],
    *,
    metric_fields: Iterable[str] = (),
    case_column: str | None = None,
    operator_column: str | None = None,
) -> dict[str, Any]:
    """Summarize launches without silently dropping the profiler signals."""
    diagnostic = ["Duration(us)", "Wait Time(us)", "Block Num", "Mix Block Num"]
    fields = list(dict.fromkeys([*diagnostic, *metric_fields]))
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[
            (
                str(row.get(operator_column) or "") if operator_column else "",
                str(row.get(case_column) or "") if case_column else "",
                row.get("Name") or row.get("Op Name") or row.get("op_name") or "?",
            )
        ].append(row)
    groups = []
    for (operator, case_id, name), kernel_rows in sorted(grouped.items()):
        metrics = {}
        for field in fields:
            values = [_number(row.get(field)) for row in kernel_rows]
            values = [value for value in values if value is not None]
            if values:
                p50 = _percentile(values, 0.50)
                metrics[field] = {
                    "n": len(values),
                    "p50": _percentile(values, 0.50),
                    "p95": _percentile(values, 0.95),
                    "p99": _percentile(values, 0.99),
                    "mad": _percentile([abs(value - p50) for value in values], 0.50),
                    "min": min(values),
                    "max": max(values),
                    "unit": "%" if any(str(row.get(field) or "").strip().endswith("%") for row in kernel_rows)
                    else ("us" if "us" in field.lower() else "raw"),
                }
        group = {"kernel": name, "launches": len(kernel_rows), "metrics": metrics}
        if operator:
            group["operator"] = operator
        if case_id:
            group["case_id"] = case_id
        groups.append(group)
    return {"groups": groups}


def audit_submitted_evidence(
    rows_path: str | Path,
    *,
    scorelog_path: str | Path | None = None,
    probe_json_path: str | Path | None = None,
    kernel_csv_path: str | Path | None = None,
    token_counter: Callable[[str], int] | None = None,
) -> dict[str, Any]:
    counter, counter_name, exact = _resolve_token_counter(token_counter)
    records = build_rows910_records(rows_path)
    score_events = 0
    if scorelog_path and Path(scorelog_path).is_file():
        score_events = sum(
            bool(re.match(r"^\|\s*2026-", line))
            for line in Path(scorelog_path).read_text(encoding="utf-8").splitlines()
        )
    probe_cases = 0
    probe_ops: list[str] = []
    if probe_json_path and Path(probe_json_path).is_file():
        payload = json.loads(Path(probe_json_path).read_text(encoding="utf-8"))
        for operator in payload.get("operators", []):
            probe_ops.append(str(operator.get("operator", "?")))
            probe_cases += len(operator.get("cases", []))

    csv_rows: list[dict[str, str]] = []
    columns: list[str] = []
    if kernel_csv_path and Path(kernel_csv_path).is_file():
        with Path(kernel_csv_path).open(newline="", encoding="utf-8-sig", errors="replace") as fh:
            reader = csv.DictReader(fh)
            columns = list(reader.fieldnames or [])
            csv_rows = list(reader)
    def is_pmu_field(column: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", "_", column.lower()).strip("_")
        markers = (
            "bandwidth",
            "_bw",
            "cache",
            "_hit",
            "_miss",
            "victim",
            "mte1",
            "mte2",
            "mte3",
            "conflict",
            "cflt",
            "stall",
            "cycles",
            "utilization",
            "_datas",
        )
        has_ratio = normalized == "ratio" or normalized.endswith("_ratio") or "_ratio_" in normalized
        return has_ratio or normalized.startswith(("aic_", "aiv_")) or any(
            marker in normalized for marker in markers
        )

    pmu_fields = [column for column in columns if is_pmu_field(column)]
    normalized_columns = {_canonical_metric(column).lower(): column for column in columns}
    case_column = next(
        (
            normalized_columns[name]
            for name in ("case_id", "caseid", "step_id", "stepid")
            if name in normalized_columns
        ),
        None,
    )
    operator_column = next(
        (
            normalized_columns[name]
            for name in ("operator", "operator_name", "op_type")
            if name in normalized_columns
        ),
        None,
    )
    has_case_key = case_column is not None

    def matching_labels(csv_row: dict[str, str]) -> list[dict[str, Any]]:
        if not case_column:
            return []
        raw_case = str(csv_row.get(case_column) or "").strip().lower()
        raw_operator = (
            str(csv_row.get(operator_column) or "").strip().lower()
            if operator_column
            else ""
        )
        if not raw_case:
            return []
        matches = []
        for record in records:
            record_case = str(record.get("case_id") or "").strip().lower()
            index = _case_index(record_case)
            aliases = {record_case}
            if index is not None:
                aliases.add(str(index))
            if raw_case not in aliases:
                continue
            if raw_operator and str(record.get("op") or "").lower() != raw_operator:
                continue
            matches.append(record)
        return matches

    numeric_pmu_rows = [
        row for row in csv_rows if any(_number(row.get(field)) is not None for field in pmu_fields)
    ]
    joinable_rows = [row for row in numeric_pmu_rows if len(matching_labels(row)) == 1]
    joinable = len(joinable_rows)

    raw_text = json.dumps(csv_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    decision_columns = list(
        dict.fromkeys(
            [
                column
                for column in (
                    operator_column,
                    case_column,
                    "Name" if "Name" in columns else None,
                    "Op Name" if "Op Name" in columns else None,
                    "Duration(us)" if "Duration(us)" in columns else None,
                    "Wait Time(us)" if "Wait Time(us)" in columns else None,
                    "Block Num" if "Block Num" in columns else None,
                    "Mix Block Num" if "Mix Block Num" in columns else None,
                    *pmu_fields,
                )
                if column
            ]
        )
    )
    lossless_decision_rows = [
        {column: row.get(column) for column in decision_columns} for row in csv_rows
    ]
    lossless_text = json.dumps(
        lossless_decision_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    compact = _compact_kernel_csv(
        csv_rows,
        metric_fields=pmu_fields,
        case_column=case_column,
        operator_column=operator_column,
    )
    compact_text = json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw_tokens = counter(raw_text) if csv_rows else 0
    lossless_tokens = counter(lossless_text) if csv_rows else 0
    compact_tokens = counter(compact_text) if csv_rows else 0
    compact_metric_fields = sorted(
        {
            field
            for group in compact.get("groups", [])
            for field in (group.get("metrics") or {})
            if field in pmu_fields
        }
    )
    evaluation = evaluate_adoption(records, legacy_fields=[], token_counter=counter)
    evidence_reason = f"{joinable} joinable labeled PMU records"
    evaluation["decision"]["adopt"] = False
    evaluation["decision"]["selected_fields"] = []
    if evidence_reason not in evaluation["decision"]["reasons"]:
        evaluation["decision"]["reasons"].insert(0, evidence_reason)

    return {
        "schema_version": SCHEMA_VERSION,
        "evidence": {
            "rows910_records": len(records),
            "rows910_operator_groups": len({row["op"] for row in records}),
            "scorelog_events": score_events,
            "probe_operators": probe_ops,
            "probe_cases": probe_cases,
            "kernel_csv_rows": len(csv_rows),
            "kernel_csv_columns": columns,
            "pmu_fields": pmu_fields,
            "numeric_pmu_rows": len(numeric_pmu_rows),
            "has_case_or_step_key": has_case_key,
            "joinable_labeled_pmu_records": joinable,
        },
        "token_audit": {
            "counter": counter_name,
            "exact": exact,
            "raw_kernel_csv_tokens": raw_tokens,
            "lossless_decision_rows_tokens": lossless_tokens,
            "decision_summary_tokens": compact_tokens,
            "lossless_decision_rows_reduction": (
                1.0 - lossless_tokens / raw_tokens if raw_tokens else None
            ),
            "decision_summary_reduction": (
                1.0 - compact_tokens / raw_tokens if raw_tokens else None
            ),
            "decision_columns": decision_columns,
            "summary_pmu_fields": compact_metric_fields,
            "all_numeric_pmu_fields_preserved": set(compact_metric_fields)
            == {
                field
                for field in pmu_fields
                if any(_number(row.get(field)) is not None for row in csv_rows)
            },
            "summary_is_lossy": True,
        },
        "adoption_evaluation": evaluation,
        "decision": evaluation["decision"],
    }


def _load_records(path: str | Path) -> list[dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _emit(payload: Any, output: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    else:
        print(text, end="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--input", required=True)
    evaluate.add_argument("--legacy-field", action="append", default=[])
    evaluate.add_argument("--require-adopt", action="store_true")
    evaluate.add_argument("--output")
    build = sub.add_parser("build-dataset")
    build.add_argument("--ab-analysis", required=True)
    build.add_argument("--profile-report", action="append", default=[])
    build.add_argument("--output")
    timing = sub.add_parser("build-timing-ab")
    timing.add_argument("--profile-report", action="append", required=True)
    timing.add_argument("--base-arm", default="base")
    timing.add_argument("--action-cell", required=True)
    timing.add_argument("--stat", default="p50", choices=("p50", "p95", "p99"))
    timing.add_argument("--output")
    audit = sub.add_parser("audit-repo")
    audit.add_argument("--rows", required=True)
    audit.add_argument("--scorelog")
    audit.add_argument("--probe-json")
    audit.add_argument("--kernel-csv")
    audit.add_argument("--output")
    args = parser.parse_args(argv)

    exit_code = 0
    if args.command == "evaluate":
        payload = evaluate_adoption(_load_records(args.input), args.legacy_field)
        if args.require_adopt and not payload.get("decision", {}).get("adopt"):
            exit_code = 6
    elif args.command == "build-dataset":
        analysis = json.loads(Path(args.ab_analysis).read_text(encoding="utf-8"))
        reports = [
            json.loads(Path(path).read_text(encoding="utf-8")) for path in args.profile_report
        ]
        payload = build_ab_profile_records(analysis, reports)
        if not payload:
            print("no valid same-run timing/profile pairs", file=sys.stderr)
            exit_code = 5
    elif args.command == "build-timing-ab":
        reports = [
            json.loads(Path(path).read_text(encoding="utf-8")) for path in args.profile_report
        ]
        payload = build_timing_ab_analysis(
            reports,
            base_arm=args.base_arm,
            action_cell=args.action_cell,
            stat=args.stat,
        )
        if not payload:
            print("no valid profiler-off timing A/B pairs", file=sys.stderr)
            exit_code = 5
    else:
        payload = audit_submitted_evidence(
            args.rows,
            scorelog_path=args.scorelog,
            probe_json_path=args.probe_json,
            kernel_csv_path=args.kernel_csv,
        )
    _emit(payload, args.output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
