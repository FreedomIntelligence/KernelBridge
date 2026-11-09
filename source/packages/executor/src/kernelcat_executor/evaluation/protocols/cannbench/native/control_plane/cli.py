"""Machine-readable CLI for KernelCat 910C agents and operators."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence import assess_evidence, evaluate_promotion
from .execution import LegacyToolAdapter
from .formal import source_tree_sha256
from .migration import migrate_trajectory, verify_backup_manifest
from .models import (
    Candidate,
    ContractError,
    EnvironmentFingerprint,
    EvidenceKind,
    EvidenceRecord,
    EvidenceStatus,
    OperatorId,
    ParentKind,
    ParentRef,
    ParentSet,
    Policy,
    as_payload,
)
from .profiler import compact_unified_feedback, compare_event_samples
from .store import ContentAddressedStore, Ledger

DEFAULT_STATE = Path(".kernelcat") / "910c"
DEFAULT_BACKUP_MANIFEST = Path("backups/cann_910c_pipeline_pre_refactor_20260826/BACKUP_MANIFEST.json")


def _json_file(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _emit(value: Any, *, pretty: bool = False) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
    )


def _state(path: str | Path) -> tuple[ContentAddressedStore, Ledger]:
    root = Path(path).expanduser().resolve()
    return ContentAddressedStore(root / "cas"), Ledger(root / "ledger.sqlite3")


def _environment(data: Mapping[str, Any]) -> EnvironmentFingerprint:
    return EnvironmentFingerprint(
        soc=data["soc"],
        compute_unit=data["compute_unit"],
        device_isa=data["device_isa"],
        cann_version=data["cann_version"],
        task_version=data["task_version"],
        toolchain_sha256=data["toolchain_sha256"],
        casepack_sha256=data["casepack_sha256"],
    )


def _parent(data: Mapping[str, Any]) -> ParentRef:
    return ParentRef(
        operator=OperatorId.parse(data["operator"]),
        source_sha256=data["source_sha256"],
        environment_id=data["environment_id"],
        kind=ParentKind(data.get("kind", ParentKind.VERIFIED_SOURCE.value)),
        candidate_id=data.get("candidate_id"),
        submission_id=data.get("submission_id"),
        score_snapshot_id=data.get("score_snapshot_id"),
        note=data.get("note", ""),
    )


def _candidate(data: Mapping[str, Any]) -> Candidate:
    parents_data = data["parents"]
    return Candidate(
        operator=OperatorId.parse(data["operator"]),
        parents=ParentSet(
            code_parent=_parent(parents_data["code_parent"]),
            decision_parent=_parent(parents_data["decision_parent"]),
            carrier_parent=_parent(parents_data["carrier_parent"]) if parents_data.get("carrier_parent") else None,
        ),
        policy_id=data["policy_id"],
        source_tree_sha256=data["source_tree_sha256"],
        task_sha256=data["task_sha256"],
        environment=_environment(data["environment"]),
        capability_predicate=data["capability_predicate"],
        fallback_entry=data["fallback_entry"],
        source_manifest_ref=data.get("source_manifest_ref"),
        labels=data.get("labels", {}),
    )


def _evidence(data: Mapping[str, Any]) -> EvidenceRecord:
    return EvidenceRecord(
        kind=EvidenceKind(data["kind"]),
        status=EvidenceStatus(data["status"]),
        candidate_id=data.get("candidate_id"),
        experiment_id=data.get("experiment_id"),
        operator=OperatorId.parse(data["operator"]),
        source_tree_sha256=data.get("source_tree_sha256"),
        casepack_sha256=data.get("casepack_sha256"),
        payload=data.get("payload", {}),
        artifact_refs=tuple(data.get("artifact_refs", ())),
        provenance=data.get("provenance", {}),
    )


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    cas, ledger = _state(args.state_dir)
    marker = cas.put_json({"schema_version": 1, "state": "initialized"})
    ledger.record_artifact(marker)
    return {
        "ok": True,
        "state_dir": str(Path(args.state_dir).expanduser().resolve()),
        "cas_marker": marker.uri,
        "ledger": ledger.health(),
    }


def command_doctor(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo_root).expanduser().resolve()
    cas, ledger = _state(args.state_dir)
    tools = repo / "native" / "r950_tools"
    expected = ("abctl", "real_profiler_ab.py", "profiler_feedback_sdk.py", "push910.py", "promotion_gate.py")
    tool_status = {name: (tools / name).is_file() for name in expected}
    backup = verify_backup_manifest(repo, args.backup_manifest).compact()
    probe = cas.put_text("kernelcat-910c-doctor\n")
    ledger.record_artifact(probe)
    health = ledger.health()
    checks = {
        "backup_exact": backup["complete"],
        "ledger_integrity": health["ok"],
        "cas_roundtrip": cas.read_bytes(probe.uri) == b"kernelcat-910c-doctor\n",
        "legacy_tools_present": all(tool_status.values()),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "backup": backup,
        "legacy_tools": tool_status,
        "ledger": health,
        "state_dir": str(Path(args.state_dir).expanduser().resolve()),
    }


def command_hash_tree(args: argparse.Namespace) -> dict[str, Any]:
    cas, ledger = _state(args.state_dir)
    root = Path(args.path).expanduser().resolve()
    manifest = cas.put_tree(root)
    ledger.record_artifact(manifest)
    return {
        "ok": True,
        "path": str(root),
        "source_tree_sha256": source_tree_sha256(root),
        "manifest_ref": manifest.uri,
        "manifest_sha256": manifest.sha256,
    }


def command_policy(args: argparse.Namespace) -> dict[str, Any]:
    cas, ledger = _state(args.state_dir)
    expected = _json_file(args.expected_signals) if args.expected_signals else {}
    policy = Policy(
        operator=OperatorId.parse(args.operator),
        mechanism_id=args.mechanism,
        hypothesis=args.hypothesis,
        applicability=tuple(args.applicability),
        fallback=args.fallback,
        expected_signals=expected,
        touched_surfaces=tuple(args.touched_surface),
        source_refs=tuple(args.source_ref),
    )
    artifact = cas.put_json(policy)
    ledger.record_artifact(artifact)
    created = ledger.record_entity(policy.policy_id, "policy", policy)
    ledger.append_event(policy.policy_id, "policy_registered", {"artifact_ref": artifact.uri})
    return {
        "ok": True,
        "created": created,
        "policy_id": policy.policy_id,
        "artifact_ref": artifact.uri,
        "policy": as_payload(policy),
    }


def command_freeze_candidate(args: argparse.Namespace) -> dict[str, Any]:
    cas, ledger = _state(args.state_dir)
    candidate = _candidate(_json_file(args.input))
    artifact = cas.put_json(candidate)
    ledger.record_artifact(artifact)
    created = ledger.record_entity(candidate.candidate_id, "candidate", candidate)
    ledger.append_event(candidate.candidate_id, "candidate_frozen", {"artifact_ref": artifact.uri})
    return {
        "ok": True,
        "created": created,
        "candidate_id": candidate.candidate_id,
        "parent_set_id": candidate.parents.parent_set_id,
        "artifact_ref": artifact.uri,
        "candidate": as_payload(candidate),
    }


def command_assess(args: argparse.Namespace) -> dict[str, Any]:
    record = _evidence(_json_file(args.input))
    return {"ok": True, "assessment": assess_evidence(record).compact()}


def command_gate(args: argparse.Namespace) -> dict[str, Any]:
    candidate = _candidate(_json_file(args.candidate))
    records: list[EvidenceRecord] = []
    for path in args.evidence:
        payload = _json_file(path)
        if isinstance(payload, list):
            records.extend(_evidence(item) for item in payload)
        else:
            records.append(_evidence(payload))
    receipt = evaluate_promotion(
        candidate,
        records,
        current_decision_parent_id=args.current_parent,
        pending_run_ids=args.pending_run,
        require_formal=args.require_formal,
        require_pmu=args.require_pmu,
    )
    if args.record:
        cas, ledger = _state(args.state_dir)
        artifact = cas.put_json(receipt)
        ledger.record_artifact(artifact)
        ledger.record_entity(receipt.gate_id, "gate_receipt", receipt)
        ledger.append_event(
            candidate.candidate_id,
            "promotion_gate_evaluated",
            {"gate_id": receipt.gate_id, "artifact_ref": artifact.uri},
        )
    return {"ok": receipt.eligible_official, "gate": receipt.compact()}


def command_event_ab(args: argparse.Namespace) -> dict[str, Any]:
    payload = _json_file(args.input)
    if payload.get("profiler_enabled") is not False:
        raise ContractError("event-ab input must explicitly set profiler_enabled=false")
    decision = compare_event_samples(
        payload["base_samples_us"],
        payload["candidate_samples_us"],
        base_build_id=payload["base_build_id"],
        candidate_build_id=payload["candidate_build_id"],
        same_device=payload.get("same_device") is True,
        profiler_enabled=False,
        correctness_pass=payload.get("correctness_pass") is True,
        case_coverage=payload.get("case_coverage", "unspecified"),
        min_samples=args.min_samples,
        min_lcb=args.min_lcb,
        max_tail_ratio=args.max_tail_ratio,
        iterations=args.iterations,
        seed=args.seed,
    )
    return {"ok": decision.accepted, "event_decision": decision.compact()}


def command_compact_profiler(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "ok": True,
        "view": compact_unified_feedback(_json_file(args.input), max_pmu_signals=args.max_pmu_signals),
    }


def command_migrate(args: argparse.Namespace) -> dict[str, Any]:
    cas, ledger = _state(args.state_dir)
    report = migrate_trajectory(args.trajectory, cas=cas, ledger=ledger, apply=args.apply)
    has_errors = any(issue.severity == "error" for issue in report.issues)
    return {"ok": not has_errors, "applied": args.apply, "report": report.compact()}


def command_verify_backup(args: argparse.Namespace) -> dict[str, Any]:
    verification = verify_backup_manifest(args.repo_root, args.manifest)
    return {"ok": verification.complete, "backup": verification.compact()}


def command_shadow_ab(args: argparse.Namespace) -> dict[str, Any]:
    adapter = LegacyToolAdapter(args.repo_root)
    request = adapter.ab20(
        operator_display=args.operator,
        base_source=args.base,
        candidate_source=args.candidate,
        tag=args.tag,
        backend=args.backend,
        npu=args.npu,
    )
    return {"ok": True, "plan": adapter.shadow_plan(request)}


def command_shadow_profiler(args: argparse.Namespace) -> dict[str, Any]:
    adapter = LegacyToolAdapter(args.repo_root)
    metrics = tuple(args.metric) if args.metric else ("PipeUtilization",)
    request = adapter.profiler(
        base_source=args.base,
        candidate_source=args.candidate,
        artifact_dir=args.artifact_dir,
        run_id=args.run_id,
        device=args.device,
        case_id=args.case_id,
        repeat=args.repeat,
        inner_repeat=args.inner_repeat,
        metric_groups=metrics,
        action_cell=args.action_cell,
        school_env=args.school_env,
    )
    return {"ok": True, "plan": adapter.shadow_plan(request)}


def command_shadow_submit(args: argparse.Namespace) -> dict[str, Any]:
    adapter = LegacyToolAdapter(args.repo_root)
    request = adapter.official_submit(operator=args.operator, source=args.source, note=args.note)
    return {
        "ok": True,
        "plan": adapter.shadow_plan(request),
        "warning": "shadow only; execution requires a hash-bound passing GateReceipt",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kcatctl", description="KernelCat 910C agent-native control plane")
    parser.add_argument("--state-dir", default=os.environ.get("KERNELCAT_910C_STATE", str(DEFAULT_STATE)))
    parser.add_argument("--pretty", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialize local CAS and ledger")
    init.set_defaults(func=command_init)

    doctor = sub.add_parser("doctor", help="verify backup, state store, and legacy adapters")
    doctor.add_argument("--repo-root", default=".")
    doctor.add_argument("--backup-manifest", default=str(DEFAULT_BACKUP_MANIFEST))
    doctor.set_defaults(func=command_doctor)

    hash_tree = sub.add_parser("hash-tree", help="freeze a source tree into CAS")
    hash_tree.add_argument("path")
    hash_tree.set_defaults(func=command_hash_tree)

    policy = sub.add_parser("create-policy", help="register one causal optimization mechanism")
    policy.add_argument("--operator", required=True)
    policy.add_argument("--mechanism", required=True)
    policy.add_argument("--hypothesis", required=True)
    policy.add_argument("--applicability", action="append", required=True)
    policy.add_argument("--fallback", required=True)
    policy.add_argument("--expected-signals")
    policy.add_argument("--touched-surface", action="append", default=[])
    policy.add_argument("--source-ref", action="append", default=[])
    policy.set_defaults(func=command_policy)

    candidate = sub.add_parser("freeze-candidate", help="register an immutable candidate JSON")
    candidate.add_argument("--input", required=True)
    candidate.set_defaults(func=command_freeze_candidate)

    assess = sub.add_parser("assess-evidence", help="explain evidence eligibility")
    assess.add_argument("--input", required=True)
    assess.set_defaults(func=command_assess)

    gate = sub.add_parser("gate", help="evaluate the official-promotion gate")
    gate.add_argument("--candidate", required=True)
    gate.add_argument("--evidence", action="append", required=True)
    gate.add_argument("--current-parent", required=True)
    gate.add_argument("--pending-run", action="append", default=[])
    gate.add_argument("--require-formal", action="store_true")
    gate.add_argument("--require-pmu", action="store_true")
    gate.add_argument("--record", action="store_true")
    gate.set_defaults(func=command_gate)

    event_ab = sub.add_parser("event-ab", help="judge profiler-off Event samples")
    event_ab.add_argument("--input", required=True)
    event_ab.add_argument("--min-samples", type=int, default=10)
    event_ab.add_argument("--min-lcb", type=float, default=1.0)
    event_ab.add_argument("--max-tail-ratio", type=float, default=1.25)
    event_ab.add_argument("--iterations", type=int, default=2000)
    event_ab.add_argument("--seed", type=int, default=0)
    event_ab.set_defaults(func=command_event_ab)

    compact = sub.add_parser("compact-profiler", help="derive a bounded decision view")
    compact.add_argument("--input", required=True)
    compact.add_argument("--max-pmu-signals", type=int, default=8)
    compact.set_defaults(func=command_compact_profiler)

    migrate = sub.add_parser("migrate-legacy", help="import TRAJECTORY.jsonl conservatively")
    migrate.add_argument("--trajectory", required=True)
    migrate.add_argument("--apply", action="store_true", help="write events; default is dry-run")
    migrate.set_defaults(func=command_migrate)

    verify = sub.add_parser("verify-backup", help="compare source and backup Git tree SHAs")
    verify.add_argument("--repo-root", default=".")
    verify.add_argument("--manifest", default=str(DEFAULT_BACKUP_MANIFEST))
    verify.set_defaults(func=command_verify_backup)

    shadow_ab = sub.add_parser("shadow-ab", help="render a legacy full20 A/B request")
    shadow_ab.add_argument("--repo-root", default=".")
    shadow_ab.add_argument("--operator", required=True)
    shadow_ab.add_argument("--base", required=True)
    shadow_ab.add_argument("--candidate", required=True)
    shadow_ab.add_argument("--tag", required=True)
    shadow_ab.add_argument("--backend", choices=("demo2", "ktp"), default="demo2")
    shadow_ab.add_argument("--npu", type=int, default=2)
    shadow_ab.set_defaults(func=command_shadow_ab)

    shadow_profiler = sub.add_parser("shadow-profiler", help="render a unified profiler request")
    shadow_profiler.add_argument("--repo-root", default=".")
    shadow_profiler.add_argument("--base", required=True)
    shadow_profiler.add_argument("--candidate", required=True)
    shadow_profiler.add_argument("--artifact-dir", required=True)
    shadow_profiler.add_argument("--run-id", required=True)
    shadow_profiler.add_argument("--device", type=int, required=True)
    shadow_profiler.add_argument("--case-id", required=True)
    shadow_profiler.add_argument("--repeat", type=int, default=30)
    shadow_profiler.add_argument("--inner-repeat", type=int, default=50)
    shadow_profiler.add_argument("--metric", action="append", default=[])
    shadow_profiler.add_argument("--action-cell", default="")
    shadow_profiler.add_argument("--school-env")
    shadow_profiler.set_defaults(func=command_shadow_profiler)

    shadow_submit = sub.add_parser("shadow-submit", help="render, but never execute, an official submission")
    shadow_submit.add_argument("--repo-root", default=".")
    shadow_submit.add_argument("--operator", required=True)
    shadow_submit.add_argument("--source", required=True)
    shadow_submit.add_argument("--note", default="")
    shadow_submit.set_defaults(func=command_shadow_submit)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        _emit(result, pretty=args.pretty)
        return 0 if result.get("ok", False) else 3
    except KeyboardInterrupt:
        _emit({"ok": False, "error": {"type": "KeyboardInterrupt", "message": "cancelled"}}, pretty=args.pretty)
        return 130
    except Exception as exc:
        _emit({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}, pretty=args.pretty)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
