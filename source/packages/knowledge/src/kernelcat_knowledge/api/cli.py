"""`kcwiki`: the recommended local, offline KernelCat Wiki interface."""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn, cast

from kernelcat_knowledge.api.application import WikiApplication
from kernelcat_knowledge.api.bootstrap import standalone_application
from kernelcat_knowledge.contracts import ContractError, canonical_json, sha256_digest
from kernelcat_knowledge.evidence.source_content import SourceContentError
from kernelcat_knowledge.generated.knowledge_runtime import (
    KnowledgeCaptureRequest,
    KnowledgeRetractionRequest,
    KnowledgeViewReference,
)
from kernelcat_knowledge.generated.python_startup import (
    ModuleAdmissionError,
    SchemaError,
)
from kernelcat_knowledge.resources.configuration import (
    RepositoryConfigError,
)
from kernelcat_knowledge.retained.control import FenceDelivery, KnowledgeControlError

EXIT_OK = 0
EXIT_ARGUMENT = 2
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4
EXIT_GATE = 5
EXIT_SOURCE = 6
EXIT_REMOTE = 7
_RUNTIME_INPUT_BYTES = 32768


class CLIError(RuntimeError):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_ARGUMENT) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CLIError("INVALID_ARGUMENTS", message, EXIT_ARGUMENT)


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    payload = {
        "schema_version": "kernelcat.cli-error/v1",
        "status": "error",
        "code": code,
        "message": message,
        **details,
    }
    payload["digest"] = sha256_digest(payload)
    return payload


def _input_text(path: str, max_bytes: int | None) -> str:
    if max_bytes is None:
        return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    if path == "-":
        raw = sys.stdin.read(max_bytes + 1).encode("utf-8")
    else:
        with Path(path).open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise CLIError("INPUT_LIMIT", f"JSON input exceeds {max_bytes} bytes", EXIT_ARGUMENT)
    return raw.decode("utf-8")


def _read_json(path: str, *, max_bytes: int | None = None) -> dict[str, Any]:
    try:
        text = _input_text(path, max_bytes)
    except OSError as exc:
        raise CLIError("INPUT_UNAVAILABLE", str(exc), EXIT_SOURCE) from exc
    except UnicodeError as exc:
        raise CLIError("INVALID_JSON", "input must use valid UTF-8", EXIT_ARGUMENT) from exc
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise CLIError("INVALID_JSON", str(exc), EXIT_ARGUMENT) from exc
    if not isinstance(payload, dict):
        raise CLIError("INVALID_JSON", "input must be one JSON object", EXIT_ARGUMENT)
    return payload


def _predicate(value: str) -> tuple[str, Any]:
    if "=" not in value:
        raise CLIError("INVALID_PREDICATE", "predicate must use key=value", EXIT_ARGUMENT)
    key, raw = value.split("=", 1)
    key = key.strip()
    if not key or not raw.strip():
        raise CLIError("INVALID_PREDICATE", "predicate key and value must be non-empty", EXIT_ARGUMENT)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return key, values if len(values) > 1 else values[0]


def _exit_for(payload: Mapping[str, Any]) -> int:
    if str(payload.get("status") or "") not in {"error", "unverified", "corrupt"}:
        return EXIT_OK
    if payload.get("schema_version") == "kernelcat.raw-result/v1":
        payload = payload["result"]
    code = str(payload.get("code") or "")
    if code in {"NOT_FOUND"}:
        return EXIT_NOT_FOUND
    if code in {
        "IDENTITY_CONFLICT", "IDEMPOTENCY_CONFLICT", "ACTIVE_REVISION_CONFLICT",
        "CORRUPT_EXISTING_DELTA",
    }:
        return EXIT_CONFLICT
    if code in {"REVIEW_REQUIRED", "PUBLISH_GATE_FAILED", "TRUST_GATE_FAILED"}:
        return EXIT_GATE
    if code in {
        "SOURCE_UNAVAILABLE", "REMOTE_SOURCE", "INTEGRITY_MISMATCH",
        "SOURCE_INTEGRITY_MISMATCH", "EVIDENCE_INTEGRITY_MISMATCH",
        "INPUT_UNAVAILABLE", "READ_MODEL_REBUILD_FAILED",
    }:
        return EXIT_SOURCE
    if code in {"REMOTE_UNAVAILABLE"}:
        return EXIT_REMOTE
    return EXIT_ARGUMENT


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="kcwiki", description="CLI-first KernelCat Wiki")
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--expected-python-modules", type=Path)
    parser.add_argument("--startup-report", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)

    search = sub.add_parser("search", help="return a V1 ContextProjection")
    search.add_argument("--request", help="KnowledgeQuery JSON path or - for stdin")
    target = search.add_mutually_exclusive_group()
    target.add_argument("--operator")
    target.add_argument("--mechanism")
    search.add_argument("--phase", default="performance")
    search.add_argument("--hardware")
    search.add_argument("--toolchain")
    search.add_argument("--bottleneck")
    search.add_argument("--question")
    search.add_argument("--task-envelope-ref")
    search.add_argument("--predicate", action="append", default=[])
    search.add_argument("--evidence-ref", action="append", default=[])
    search.add_argument("--device-feedback")
    search.add_argument("--seen-id", action="append", default=[])
    search.add_argument("--limit", type=int, default=6)
    search.add_argument("--token-budget", type=int, default=1200)

    show = sub.add_parser("show", help="show a card/page by logical or revision ID")
    show.add_argument("id")
    show.add_argument("--view", choices=["agent", "audit", "human"], default="agent")
    show.add_argument("--max-chars", type=int, default=12000)

    source = sub.add_parser("source")
    source_sub = source.add_subparsers(dest="source_command", required=True, parser_class=JsonArgumentParser)
    source_register = source_sub.add_parser("register")
    source_register.add_argument("--input", required=True)
    source_verify = source_sub.add_parser("verify")
    source_verify.add_argument("source_id")
    source_show = source_sub.add_parser("show")
    source_show.add_argument("source_id")
    source_list = source_sub.add_parser("list")
    source_list.add_argument("--class", dest="source_class")

    evidence = sub.add_parser("evidence")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True, parser_class=JsonArgumentParser)
    evidence_register = evidence_sub.add_parser("register")
    evidence_register.add_argument("--input", required=True)
    evidence_show = evidence_sub.add_parser("show")
    evidence_show.add_argument("evidence_id")

    raw = sub.add_parser("raw")
    raw_sub = raw.add_subparsers(dest="raw_command", required=True, parser_class=JsonArgumentParser)
    raw_grep = raw_sub.add_parser("grep")
    scope = raw_grep.add_mutually_exclusive_group()
    scope.add_argument("--source")
    scope.add_argument("--scope")
    raw_grep.add_argument("--pattern", required=True)
    raw_grep.add_argument("--context", type=int, default=4)
    raw_grep.add_argument("--glob")
    raw_grep.add_argument("--include-tests", action="store_true")
    raw_grep.add_argument("--max-hits", type=int, default=30)
    raw_read = raw_sub.add_parser("read")
    target_read = raw_read.add_mutually_exclusive_group(required=True)
    target_read.add_argument("--path")
    target_read.add_argument("--evidence")
    raw_read.add_argument("--start", type=int, default=1)
    raw_read.add_argument("--end", type=int)
    raw_read.add_argument("--selector", help="selector override JSON")

    trace = sub.add_parser("trace")
    trace.add_argument("id")

    card = sub.add_parser("card")
    card_sub = card.add_subparsers(dest="card_command", required=True, parser_class=JsonArgumentParser)
    card_validate = card_sub.add_parser("validate")
    card_validate.add_argument("--input", required=True)
    card_diff = card_sub.add_parser("diff")
    card_diff.add_argument("--input", required=True)
    card_publish = card_sub.add_parser("publish")
    card_publish.add_argument("--input", required=True)
    card_publish.add_argument("--reviewed", action="store_true")
    card_show = card_sub.add_parser("show")
    card_show.add_argument("id")
    card_show.add_argument("--view", choices=["agent", "audit", "human"], default="audit")

    trajectory = sub.add_parser("trajectory")
    trajectory_sub = trajectory.add_subparsers(dest="trajectory_command", required=True, parser_class=JsonArgumentParser)
    trajectory_append = trajectory_sub.add_parser("append")
    trajectory_append.add_argument("--input", required=True)

    outbox = sub.add_parser("outbox")
    outbox_sub = outbox.add_subparsers(dest="outbox_command", required=True, parser_class=JsonArgumentParser)
    outbox_sub.add_parser("status")

    index = sub.add_parser("index")
    index_sub = index.add_subparsers(dest="index_command", required=True, parser_class=JsonArgumentParser)
    index_sub.add_parser("rebuild")
    index_sub.add_parser("status")

    validate = sub.add_parser("validate")
    validate.add_argument("--input", required=True)

    sub.add_parser("doctor")

    schema = sub.add_parser("schema")
    schema.add_argument("name", help="schema stem, e.g. knowledge-query")

    snapshot = sub.add_parser("snapshot")
    snapshot_sub = snapshot.add_subparsers(dest="snapshot_command", required=True, parser_class=JsonArgumentParser)
    snapshot_create = snapshot_sub.add_parser("create")
    snapshot_create.add_argument("--output", type=Path, required=True)

    _retained_parser(sub)

    return parser


def _retained_parser(sub: Any) -> None:
    control = sub.add_parser("control", help="trusted Knowledge control administration")
    control.add_subparsers(dest="control_command", required=True).add_parser("status")
    capture = sub.add_parser("capture", help="register one trusted consumer and retain its exact view")
    capture.add_argument("--request", required=True)
    view = sub.add_parser("view", help="read an exact retained reference")
    view.add_argument("--reference", required=True)
    reads = view.add_subparsers(dest="view_command", required=True)
    search = reads.add_parser("search")
    search.add_argument("--request", required=True)
    show = reads.add_parser("show")
    show.add_argument("id")
    show.add_argument("--view", choices=["agent", "audit", "human"], default="agent")
    show.add_argument("--max-chars", type=int, default=12000)
    source = reads.add_parser("source")
    source.add_argument("source_id")
    raw = reads.add_parser("read")
    raw.add_argument("--evidence", required=True)
    raw.add_argument("--selector")
    retraction = sub.add_parser("retraction", help="trusted permanent deny and fence administration")
    commands = retraction.add_subparsers(dest="retraction_command", required=True)
    begin = commands.add_parser("begin")
    begin.add_argument("--request", required=True)
    for name in ("status", "pending", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("intent_id")
        if name != "status":
            command.add_argument("--limit", type=int, default=100)
        if name == "pending":
            command.add_argument("--cursor")


def _query_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.request:
        return _read_json(args.request)
    if not args.operator and not args.mechanism:
        raise CLIError("MISSING_QUERY_TARGET", "--operator or --mechanism is required", EXIT_ARGUMENT)
    if not args.bottleneck or not args.question:
        raise CLIError("MISSING_QUERY_FIELD", "--bottleneck and --question are required", EXIT_ARGUMENT)
    predicates: dict[str, Any] = {}
    for raw in args.predicate:
        key, value = _predicate(raw)
        if key in predicates:
            raise CLIError("DUPLICATE_PREDICATE", f"predicate {key!r} repeated", EXIT_ARGUMENT)
        predicates[key] = value
    query: dict[str, Any] = {
        "schema_version": "kernelcat.knowledge-query/v1",
        "phase": args.phase,
        "bottleneck": args.bottleneck,
        "decision_question": args.question,
        "predicates": predicates,
        "evidence_refs": list(args.evidence_ref),
        "seen_ids": list(args.seen_id),
        "limit": args.limit,
        "token_budget": args.token_budget,
    }
    query["operator_id" if args.operator else "mechanism_family"] = args.operator or args.mechanism
    for key, value in (
        ("hardware", args.hardware),
        ("toolchain", args.toolchain),
        ("task_envelope_ref", args.task_envelope_ref),
        ("device_feedback", args.device_feedback),
    ):
        if value is not None:
            query[key] = value
    return query


def dispatch(args: argparse.Namespace, *, app: WikiApplication, deliver_fence: FenceDelivery | None = None) -> dict[str, Any]:
    root = app.repo_root
    if args.command == "control":
        return dict(app.control_status())
    if args.command == "capture":
        request = cast(
            KnowledgeCaptureRequest,
            _read_json(args.request, max_bytes=_RUNTIME_INPUT_BYTES),
        )
        return dict(app.capture_consumer(request))
    if args.command == "view":
        return _retained_read(args, app)
    if args.command == "retraction":
        return _retraction(args, app, deliver_fence)
    if args.command == "search":
        return app.search(_query_from_args(args))
    if args.command == "show":
        return app.show(args.id, view=args.view, max_chars=args.max_chars)
    if args.command == "source":
        if args.source_command == "register":
            return app.source_register(_read_json(args.input))
        if args.source_command == "verify":
            return app.source_verify(args.source_id)
        if args.source_command == "show":
            return app.source_show(args.source_id)
        return app.source_list(source_class=args.source_class)
    if args.command == "evidence":
        if args.evidence_command == "register":
            return app.evidence_register(_read_json(args.input))
        return app.evidence_show(args.evidence_id)
    if args.command == "raw":
        if args.raw_command == "grep":
            return app.raw_grep(
                pattern=args.pattern,
                source_id=args.source,
                scope=args.scope,
                context=args.context,
                glob=args.glob,
                include_tests=args.include_tests,
                max_hits=args.max_hits,
            )
        selector = _read_json(args.selector) if args.selector else None
        return app.raw_read(
            path=args.path,
            evidence_id=args.evidence,
            start=args.start,
            end=args.end,
            selector_override=selector,
        )
    if args.command == "trace":
        return app.trace(args.id)
    if args.command == "card":
        if args.card_command == "show":
            return app.show(args.id, view=args.view)
        payload = _read_json(args.input)
        if args.card_command == "validate":
            return app.card_validate(payload)
        if args.card_command == "diff":
            return app.card_diff(payload)
        return app.card_publish(payload, reviewed=args.reviewed)
    if args.command == "trajectory":
        return app.append_trajectory(_read_json(args.input))
    if args.command == "outbox":
        return app.outbox_status()
    if args.command == "index":
        return app.index_rebuild() if args.index_command == "rebuild" else app.index_status()
    if args.command == "validate":
        return app.validate_object(_read_json(args.input))
    if args.command == "doctor":
        return app.doctor()
    if args.command == "schema":
        path = root / "wiki/schemas" / f"{args.name}.schema.json"
        if not path.is_file():
            raise CLIError("NOT_FOUND", f"schema not found: {args.name}", EXIT_NOT_FOUND)
        schema = json.loads(path.read_text(encoding="utf-8"))
        payload = {
            "schema_version": "kernelcat.schema-result/v1",
            "status": "ok",
            "name": args.name,
            "schema": schema,
        }
        payload["digest"] = sha256_digest(payload)
        return payload
    if args.command == "snapshot":
        return app.snapshot_create(args.output)
    raise CLIError("UNKNOWN_COMMAND", str(args.command), EXIT_ARGUMENT)


def _retained_read(args: argparse.Namespace, app: WikiApplication) -> dict[str, Any]:
    reference = cast(
        KnowledgeViewReference,
        _read_json(args.reference, max_bytes=_RUNTIME_INPUT_BYTES),
    )
    view = app.open_view(reference)
    if args.view_command == "search":
        return view.search(_read_json(args.request))
    if args.view_command == "show":
        return view.show(args.id, view=args.view, max_chars=args.max_chars)
    if args.view_command == "source":
        return view.source_show(args.source_id)
    return view.raw_read(
        evidence_id=args.evidence,
        selector_override=_read_json(args.selector) if args.selector else None,
    )


def _retraction(
    args: argparse.Namespace, app: WikiApplication, deliver_fence: FenceDelivery | None
) -> dict[str, Any]:
    if args.retraction_command == "begin":
        request = cast(
            KnowledgeRetractionRequest,
            _read_json(args.request, max_bytes=_RUNTIME_INPUT_BYTES),
        )
        return dict(app.begin_retraction(request))
    if args.retraction_command == "status":
        return dict(app.retraction_status(args.intent_id))
    if args.retraction_command == "pending":
        return dict(app.pending_fences(args.intent_id, cursor=args.cursor, limit=args.limit))
    if deliver_fence is None:
        raise KnowledgeControlError(
            "TRUSTED_FENCE_DELIVERY_REQUIRED",
            "CLI composition has no original-Journal fence delivery",
            status=app.retraction_status(args.intent_id),
        )
    return dict(app.reconcile_retraction(args.intent_id, deliver_fence, limit=args.limit))


def run(
    argv: Sequence[str] | None = None, *, expected_modules: object | None = None,
    deliver_fence: FenceDelivery | None = None
) -> tuple[int, dict[str, Any]]:
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
        app = standalone_application(
            args.repo_root, expected_modules=expected_modules,
            binding_path=args.expected_python_modules, startup_report=args.startup_report,
        )
        payload = dispatch(args, app=app, deliver_fence=deliver_fence)
        return _exit_for(payload), payload
    except CLIError as exc:
        return exc.exit_code, _error(exc.code, str(exc))
    except (ModuleAdmissionError, SchemaError, json.JSONDecodeError, UnicodeError) as exc:
        return EXIT_ARGUMENT, _error("MODULE_ADMISSION_FAILED", str(exc))
    except RepositoryConfigError as exc:
        return EXIT_ARGUMENT, _error("RESOURCE_ROOT_REQUIRED", str(exc))
    except KnowledgeControlError as exc:
        details = {"retraction_status": dict(exc.status)} if exc.status is not None else {}
        payload = _error(exc.code, str(exc), **details)
        if exc.code in {"IDEMPOTENCY_CONFLICT", "CONSUMER_CONFLICT", "CONTROL_REVISION_CONFLICT", "AUTHORITY_CONFLICT"}:
            code = EXIT_CONFLICT
        elif exc.code in {"NOT_FOUND", "VIEW_NOT_FOUND"}:
            code = EXIT_NOT_FOUND
        else:
            code = EXIT_GATE
        return code, payload
    except SourceContentError as exc:
        return EXIT_SOURCE, _error(exc.code, str(exc))
    except ContractError as exc:
        return EXIT_ARGUMENT, _error("INVALID_CONTRACT", str(exc))
    except FileNotFoundError as exc:
        return EXIT_NOT_FOUND, _error("NOT_FOUND", str(exc))
    except PermissionError as exc:
        return EXIT_SOURCE, _error("SOURCE_UNAVAILABLE", str(exc))
    except OSError as exc:
        return EXIT_SOURCE, _error("IO_ERROR", str(exc))


def main(
    argv: Sequence[str] | None = None, *, expected_modules: object | None = None,
) -> int:
    code, payload = run(argv, expected_modules=expected_modules)
    sys.stdout.write(canonical_json(payload) + "\n")
    if code:
        sys.stderr.write(f"kcwiki: {payload.get('code')}: {payload.get('message', payload.get('status'))}\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
