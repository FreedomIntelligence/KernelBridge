#!/usr/bin/env python3
"""Build a compact, non-duplicative CANNBench + directed-PMU feedback bundle."""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Callable


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _case_index(value: Any) -> int | None:
    match = re.search(r"(\d+)$", str(value or ""))
    return int(match.group(1)) if match else None


def _operator_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _case_selector(value: tuple[str, Any] | str) -> tuple[str, int]:
    if isinstance(value, str):
        operator, separator, case_id = value.partition(":")
        if not separator:
            raise ValueError(
                f"invalid case selector {value!r}; expected OPERATOR:CASE_ID"
            )
    else:
        operator, case_id = value
    operator_key = _operator_key(operator)
    case_index = _case_index(case_id)
    if not operator_key or case_index is None:
        raise ValueError(
            f"invalid case selector {value!r}; expected OPERATOR:CASE_ID"
        )
    return operator_key, case_index


def _compact_accuracy(case: dict[str, Any]) -> dict[str, Any]:
    accuracy = case.get("accuracy") or {}
    metadata = accuracy.get("metadata") or {}
    result = {"passed": accuracy.get("passed")}
    mapping = {
        "checker_name": "checker",
        "threshold": "threshold",
        "mismatch_ratio": "mismatch_ratio",
        "max_diff": "max_diff",
    }
    if accuracy.get("passed") is False:
        mapping["mean_diff"] = "mean_diff"
    for source, target in mapping.items():
        value = metadata.get(source, accuracy.get(source))
        if value is not None:
            result[target] = value
    error = accuracy.get("error_msg") or case.get("error_msg")
    if error:
        result["error"] = str(error)
    return result


def _compact_execution(case: dict[str, Any], top_kernels: int) -> dict[str, Any]:
    kernels = ((case.get("op_times") or {}).get("device_kernels") or {})
    numeric = [
        (str(name), value)
        for name, raw in kernels.items()
        if (value := _number(raw)) is not None and value >= 0
    ]
    numeric.sort(key=lambda item: (-item[1], item[0]))
    total = sum(value for _, value in numeric)
    elapsed = _number(case.get("elapsed_us"))
    result: dict[str, Any] = {
        "kernel_count": len(numeric),
        "kernel_total_us": total,
        "top_kernels": [
            {
                "name": name,
                "us": value,
                "share": value / total if total > 0 else None,
            }
            for name, value in numeric[:top_kernels]
        ],
    }
    if elapsed is not None and elapsed > 0:
        gap = max(0.0, elapsed - total)
        if gap > 0:
            result["non_kernel_gap_us"] = gap
            result["launch_bound_ratio"] = gap / elapsed
    return result


def _profile_index(
    reports: list[dict[str, Any]], selected_fields: set[str] | None, max_fields: int
) -> tuple[dict[tuple[str, int], dict[str, Any]], list[dict[str, Any]]]:
    index: dict[tuple[str, int], dict[str, Any]] = {}
    cells: dict[tuple[str, ...], dict[str, Any]] = {}
    for report in reports:
        metadata = report.get("metadata") or {}
        if (
            report.get("schema_version") != "pipeprof/v1"
            or report.get("capture_kind") != "pmu"
            or metadata.get("patch_verified") is not True
        ):
            continue
        operator = _operator_key(metadata.get("operator"))
        case_id = _case_index(metadata.get("case_id"))
        if not operator or case_id is None:
            continue
        group = str(report.get("metric_group") or "unknown")
        weighted = report.get("duration_weighted_metrics") or {}
        metrics = []
        for field, stats in sorted(weighted.items()):
            qualified = f"{group}.{field}"
            if selected_fields is not None and qualified not in selected_fields:
                continue
            value = _number((stats or {}).get("duration_weighted_mean"))
            if value is not None:
                metrics.append((qualified, value, (stats or {}).get("aggregation")))
        metrics = metrics[:max_fields]
        context = {}
        for field, stats in (report.get("context_metrics") or {}).items():
            normalized = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
            canonical = {
                "wait_time": "wait_time_us",
                "wait_time_us": "wait_time_us",
                "block_num": "block_num",
                "mix_block_num": "mix_block_num",
            }.get(normalized)
            if canonical is None:
                continue
            compact_stats = {
                name: (stats or {}).get(name)
                for name in ("p50", "p95_p50_ratio", "mad")
                if (stats or {}).get(name) is not None
            }
            if compact_stats:
                context[canonical] = compact_stats
        report_available = report.get("available") is True
        counters_available = report_available and bool(metrics)
        if counters_available:
            counter_status = "available"
        elif not report_available:
            counter_status = "unavailable"
        elif selected_fields is not None and report.get("duration_weighted_metrics"):
            counter_status = "not_selected"
        else:
            counter_status = "unavailable"
        item = {
            "metric_group": group,
            "capture_kind": "pmu",
            "counter_status": counter_status,
            "counter_available": counters_available,
        }
        if counters_available:
            item["fields"] = {field: value for field, value, _ in metrics}
            item["aggregation"] = sorted(
                {aggregation for _, _, aggregation in metrics if aggregation}
            )
        else:
            item["context_only"] = bool(context)
            reason = report.get("unavailable_reason")
            if reason:
                item["unavailable_reason"] = str(reason)
            elif counter_status == "not_selected":
                item["unavailable_reason"] = "no requested weighted counter selected"
            else:
                item["unavailable_reason"] = "no safe duration-weighted counter"
        if context:
            item["scheduler_context"] = context
        key = (operator, case_id)
        bucket = index.setdefault(key, {"captures": []})
        if any(existing.get("metric_group") == group for existing in bucket["captures"]):
            raise ValueError(f"duplicate profiler capture for {operator} case {case_id} {group}")
        bucket["captures"].append(item)
        cell_fields = ("soc", "cann_version", "compiler_version", "dtype", "shape_domain")
        cell = tuple(str(metadata.get(field) or "") for field in cell_fields)
        if all(cell):
            cells[cell] = dict(zip(cell_fields, cell))
    return index, [cells[key] for key in sorted(cells)]


def compact_feedback(
    cannbench: dict[str, Any],
    profile_reports: list[dict[str, Any]] | None = None,
    *,
    selected_fields: list[str] | None = None,
    selected_operators: list[str] | None = None,
    selected_cases: list[tuple[str, Any] | str] | None = None,
    top_kernels: int = 3,
    max_profile_fields: int = 6,
) -> dict[str, Any]:
    reports = profile_reports or []
    operator_filter = (
        {_operator_key(operator) for operator in selected_operators}
        if selected_operators is not None
        else None
    )
    if operator_filter is not None and "" in operator_filter:
        raise ValueError("operator selector cannot be empty")
    case_filter = (
        {_case_selector(selector) for selector in selected_cases}
        if selected_cases is not None
        else None
    )
    selected_reports = []
    for report in reports:
        metadata = report.get("metadata") or {}
        report_operator = _operator_key(metadata.get("operator"))
        report_case = _case_index(metadata.get("case_id"))
        if operator_filter is not None and report_operator not in operator_filter:
            continue
        if case_filter is not None and (report_operator, report_case) not in case_filter:
            continue
        selected_reports.append(report)
    profile_index, profile_cells = _profile_index(
        selected_reports,
        set(selected_fields) if selected_fields is not None else None,
        max_profile_fields,
    )
    cases = []
    source_operators: set[str] = set()
    selected_operator_keys: set[str] = set()
    matched_case_selectors: set[tuple[str, int]] = set()
    for operator in cannbench.get("operators") or []:
        operator_name = str(operator.get("operator") or "")
        operator_key = _operator_key(operator_name)
        source_operators.add(operator_key)
        if operator_filter is not None and operator_key not in operator_filter:
            continue
        for case in operator.get("cases") or []:
            case_id = _case_index(case.get("case_id"))
            selector = (operator_key, case_id)
            if case_filter is not None and selector not in case_filter:
                continue
            if case_id is not None:
                matched_case_selectors.add((operator_key, case_id))
            selected_operator_keys.add(operator_key)
            timing = {}
            elapsed = _number(case.get("elapsed_us"))
            if elapsed is not None and elapsed > 0:
                timing["elapsed_us"] = elapsed
            for field in ("baseline_perf_us", "t_hw_us", "speedup"):
                value = _number(case.get(field))
                if value is not None and value > 0:
                    timing[field] = value
            item: dict[str, Any] = {
                "operator": operator_name,
                "case_id": case.get("case_id"),
                "status": case.get("status"),
                "correctness": _compact_accuracy(case),
                "golden_timing": timing,
                "execution": _compact_execution(case, top_kernels),
            }
            profiler = profile_index.get((operator_key, case_id))
            if profiler:
                item["profiler"] = profiler
            cases.append(item)

    if operator_filter is not None:
        missing_operators = sorted(operator_filter - source_operators)
        if missing_operators:
            raise ValueError(f"operator selector(s) not found: {', '.join(missing_operators)}")
    if case_filter is not None:
        missing_cases = sorted(case_filter - matched_case_selectors)
        if missing_cases:
            formatted = ", ".join(f"{operator}:{case_id}" for operator, case_id in missing_cases)
            raise ValueError(f"case selector(s) not found: {formatted}")

    kernel_names = sorted(
        {
            kernel["name"]
            for case in cases
            for kernel in (case.get("execution") or {}).get("top_kernels", [])
        }
    )
    kernel_ids = {name: f"k{index}" for index, name in enumerate(kernel_names, start=1)}
    for case in cases:
        for kernel in (case.get("execution") or {}).get("top_kernels", []):
            kernel["id"] = kernel_ids[kernel.pop("name")]

    top_summary_fields = (
        "framework_version",
        "tasks_version",
        "timestamp",
        "device",
        "total_operators",
        "total_cases",
        "passed_cases",
        "failed_cases",
        "overall_score",
        "result_stage",
    )
    selection: dict[str, Any] = {
        "selected_operator_count": len(selected_operator_keys),
        "selected_case_count": len(cases),
    }
    if operator_filter is not None:
        selection["operator_filter"] = sorted(operator_filter)
    if case_filter is not None:
        selection["case_filter"] = [
            {"operator": operator, "case_id": case_id}
            for operator, case_id in sorted(case_filter)
        ]

    return {
        "schema_version": "ascend-profiler-feedback/v1",
        "roles": {
            "golden_timing": "CANNBench result layer",
            "profiler": "mechanism attribution only; profiler duration is excluded",
            "profiler_off_event": "authoritative A/B reward when attached separately",
        },
        "summary": {
            field: cannbench.get(field)
            for field in top_summary_fields
            if cannbench.get(field) is not None
        },
        "selection": selection,
        "profile_provenance_cells": profile_cells,
        "kernel_dictionary": {kernel_ids[name]: name for name in kernel_names},
        "cases": cases,
    }


def token_audit(
    cannbench: dict[str, Any],
    profile_reports: list[dict[str, Any]],
    compact: dict[str, Any],
    token_counter: Callable[[str], int],
) -> dict[str, Any]:
    raw = json.dumps(
        {"cannbench": cannbench, "profile_reports": profile_reports},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    packed = json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw_tokens = token_counter(raw)
    compact_tokens = token_counter(packed)
    return {
        "scope": "serialized feedback payload only",
        "raw_scope": (
            "all supplied CANNBench and profile-report JSON, including source rows "
            "removed by compact selection"
        ),
        "compact_scope": "compact JSON before this token_audit field is attached",
        "excluded_scope": "optimizer prompt scaffolding and optimizer output tokens",
        "raw_tokens": raw_tokens,
        "compact_tokens": compact_tokens,
        "reduction": 1.0 - compact_tokens / raw_tokens if raw_tokens else None,
    }


def _token_counter() -> tuple[Callable[[str], int], str]:
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("o200k_base")
        return lambda text: len(encoding.encode(text)), "tiktoken:o200k_base"
    except Exception:
        return lambda text: max(1, (len(text.encode("utf-8")) + 3) // 4), "utf8-bytes/4"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cannbench", required=True)
    parser.add_argument("--profile-report", action="append", default=[])
    parser.add_argument("--selected-field", action="append")
    parser.add_argument(
        "--operator",
        action="append",
        dest="selected_operators",
        help="include all cases for this operator (repeatable, case-insensitive)",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="selected_cases",
        metavar="OPERATOR:CASE_ID",
        help="include this exact operator/case pair (repeatable)",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    cannbench = json.loads(Path(args.cannbench).read_text(encoding="utf-8"))
    reports = [
        json.loads(Path(path).read_text(encoding="utf-8")) for path in args.profile_report
    ]
    try:
        compact = compact_feedback(
            cannbench,
            reports,
            selected_fields=args.selected_field,
            selected_operators=args.selected_operators,
            selected_cases=args.selected_cases,
        )
    except ValueError as exc:
        parser.error(str(exc))
    counter, counter_name = _token_counter()
    compact["token_audit"] = {
        "counter": counter_name,
        **token_audit(cannbench, reports, compact, counter),
    }
    output = Path(args.output)
    if output.exists():
        print(f"refusing existing output: {output}")
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
