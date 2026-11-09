#!/usr/bin/env python3
"""Metric collection and Stage-1 compatibility hooks for profiler_run."""
from __future__ import annotations

import json
import re
import shlex
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

import profiler_run_core as _core

SUPPORTED_METRIC_GROUPS = (
    "PipeUtilization",
    "Memory",
    "MemoryAccess",
    "MemoryUB",
    "ResourceConflictRatio",
    "L2Cache",
    "MemoryL0",
    "ArithmeticUtilization",
)
DEFAULT_DIAGNOSTIC_METRICS = SUPPORTED_METRIC_GROUPS
METRIC_PLAN_SCHEMA = "kernelcat.pmu-collection-plan/v1"

_PATCHABLE_CORE_NAMES = (
    "_precheck",
    "_upload",
    "_setup_remote",
    "_confirm_no_evaluation",
    "_collect_remote",
    "query_completed_run",
    "serialize_feedback",
    "build_agent_projection",
    "serialize_projection",
)


def dedupe_metrics(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        value = str(value).strip()
        if value not in SUPPORTED_METRIC_GROUPS:
            supported = ", ".join(SUPPORTED_METRIC_GROUPS)
            raise ValueError(f"unsupported --metric {value!r}; choose one of: {supported}")
        if value not in result:
            result.append(value)
    return tuple(result)


def strip_metrics(argv: list[str]) -> tuple[list[str], tuple[str, ...], bool]:
    cleaned: list[str] = []
    explicit: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--metric":
            if index + 1 >= len(argv):
                raise ValueError("--metric requires a value")
            explicit.append(argv[index + 1])
            index += 2
            continue
        if item.startswith("--metric="):
            explicit.append(item.split("=", 1)[1])
            index += 1
            continue
        cleaned.append(item)
        index += 1
    metrics = dedupe_metrics(explicit or list(DEFAULT_DIAGNOSTIC_METRICS))
    return cleaned, metrics, bool(explicit)


def option_value(argv: list[str], name: str) -> str:
    for index, item in enumerate(argv):
        if item == name:
            if index + 1 >= len(argv):
                raise ValueError(f"{name} requires a value")
            return argv[index + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    raise ValueError(f"missing required option: {name}")


def replace_option(argv: list[str], name: str, value: str) -> list[str]:
    result = list(argv)
    for index, item in enumerate(result):
        if item == name:
            if index + 1 >= len(result):
                raise ValueError(f"{name} requires a value")
            result[index + 1] = value
            return result
        if item.startswith(name + "="):
            result[index] = f"{name}={value}"
            return result
    raise ValueError(f"missing required option: {name}")


def metric_slug(metric: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", metric).strip("_").lower()


def multi_pmu_command(
    args,
    *,
    remote_run: str,
    task_rel: str,
    metrics: tuple[str, ...],
    arm: str,
    source_name: str,
) -> str:
    commands = [
        _core._common_env(remote_run),
        f"export PATH={shlex.quote(remote_run + '/venv/bin')}:$PATH; ",
        f"cd {shlex.quote(remote_run)}; status=0; ",
    ]
    for metric in metrics:
        metric_dir = f"{remote_run}/pmu_{arm}/{metric_slug(metric)}"
        commands.append(
            f"echo METRIC_BEGIN group={shlex.quote(metric)} arm={shlex.quote(arm)}; "
            f"if python3 pipeprof.py {shlex.quote(args.operator)} "
            f"{shlex.quote(remote_run + '/' + source_name)} "
            f"--bench {shlex.quote(args.bench)} --dev {args.device} "
            f"--metric {shlex.quote(metric)} --level Level1 "
            f"--out {shlex.quote(metric_dir + '/reports')} "
            f"--json-out {shlex.quote(metric_dir + '/pipeprof.json')} "
            f"--arm {shlex.quote(arm)} --case-id {args.case_id} "
            f"--task-rel {shlex.quote(task_rel)} --run-id {shlex.quote(args.run_id)} "
            f"--soc {shlex.quote(args.soc)} --cann-version {shlex.quote(args.cann_version)} "
            f"--compiler-version {shlex.quote(args.compiler_version)} "
            f"--dtype {shlex.quote(args.dtype)} --shape-domain {shlex.quote(args.shape_domain)}; "
            "then rc=0; else rc=$?; fi; "
            f"echo METRIC_END group={shlex.quote(metric)} arm={shlex.quote(arm)} rc=$rc; "
            "if test $rc -ne 0; then status=$rc; fi; "
        )
    commands.append('exit "$status"')
    return "".join(commands)


@contextmanager
def patched_core(
    metrics: tuple[str, ...],
    *,
    explicit_metrics: bool,
    surface: Mapping[str, Any],
) -> Iterator[None]:
    originals: dict[str, Any] = {}
    for name in _PATCHABLE_CORE_NAMES:
        if name in surface:
            originals[name] = getattr(_core, name)
            setattr(_core, name, surface[name])

    original_identity = _core._experiment_identity
    original_event = _core._event_command
    original_pmu = _core._pmu_command
    source_identity: dict[str, str] = {}

    def experiment_identity(args, *, source_hashes, snake, metric):
        _, payload = original_identity(
            args, source_hashes=source_hashes, snake=snake, metric=metric
        )
        source_identity.update({str(key): str(value) for key, value in source_hashes.items()})
        payload["pmu_metric_groups"] = list(metrics)
        payload["pmu_collection_plan"] = {
            "schema_version": METRIC_PLAN_SCHEMA,
            "mode": "explicit" if explicit_metrics else "default_diagnostic_pack",
            "groups": list(metrics),
            "one_group_per_profiler_invocation": True,
        }
        return _core._content_id("experiment", payload), payload

    def event_command(args, *, remote_run, arm, source_name, sequence):
        command = original_event(
            args,
            remote_run=remote_run,
            arm=arm,
            source_name=source_name,
            sequence=sequence,
        )
        source_sha = source_identity.get("base" if arm == "base" else "mt", "")
        context = {
            "schema_version": "kernelcat.route-context/v1",
            "run_id": args.run_id,
            "case_id": args.case_id,
            "arm": arm,
            "action_cell": args.action_cell,
            "source_tree_sha256": source_sha,
        }
        compact = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        prefix = (
            f"export KERNELCAT_ROUTE_CONTEXT={shlex.quote(compact)} "
            f"KERNELCAT_SOURCE_SHA256={shlex.quote(source_sha)} "
            f"KERNELCAT_ACTION_CELL={shlex.quote(args.action_cell)}; "
            f"echo KERNELCAT_BUILD_IDENTITY {shlex.quote(compact)}; "
        )
        return prefix + command

    def pmu_command(args, *, remote_run, task_rel, metric, arm, source_name):
        del metric
        return multi_pmu_command(
            args,
            remote_run=remote_run,
            task_rel=task_rel,
            metrics=metrics,
            arm=arm,
            source_name=source_name,
        )

    _core._experiment_identity = experiment_identity
    _core._event_command = event_command
    _core._pmu_command = pmu_command
    try:
        yield
    finally:
        _core._experiment_identity = original_identity
        _core._event_command = original_event
        _core._pmu_command = original_pmu
        for name, value in originals.items():
            setattr(_core, name, value)
