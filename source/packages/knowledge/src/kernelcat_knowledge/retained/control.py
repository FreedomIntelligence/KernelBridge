"""Knowledge-owned capture, deny admission and trusted finite fence reconciliation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from itertools import islice
from typing import Any, cast

from kernelcat_knowledge.contracts import ContractError, canonical_json
from kernelcat_knowledge.contracts.runtime import (
    MAX_SAFE_INTEGER,
    validate_runtime,
    wire_bytes,
    wire_digest,
)
from kernelcat_knowledge.generated.knowledge_runtime import (
    KnowledgeCaptureReceipt,
    KnowledgeCaptureRequest,
    KnowledgeConsumerStatus,
    KnowledgeControlStatus,
    KnowledgeFencePage,
    KnowledgeFenceReceipt,
    KnowledgeFenceRequest,
    KnowledgeRegistration,
    KnowledgeRetraction,
    KnowledgeRetractionRequest,
    KnowledgeRetractionStatus,
    KnowledgeViewReference,
)
from kernelcat_knowledge.retained.view import RetainedKnowledgeView
from kernelcat_knowledge.storage.retained import RetainedStore
from kernelcat_knowledge.storage.selection import (
    apply_deny_overlay,
    blocked_cards,
    known_dependency,
    live_metadata,
    retain_selection,
    select_metadata,
    writer_boundary,
)

FenceDelivery = Callable[[KnowledgeFenceRequest], KnowledgeFenceReceipt]


class KnowledgeControlError(ContractError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: KnowledgeRetractionStatus | KnowledgeConsumerStatus | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _body(value: Any) -> str:
    return wire_bytes(value).decode("utf-8")


def _revision(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT revision FROM header").fetchone()[0])


def _cas(connection: sqlite3.Connection | None, expected: int) -> None:
    if (0 if connection is None else _revision(connection)) != expected:
        raise KnowledgeControlError(
            "CONTROL_REVISION_CONFLICT", "Knowledge control revision changed"
        )


def _capture_row(connection: sqlite3.Connection, intent: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM captures WHERE intent=?", (intent,)
    ).fetchone()
    if row is None:
        raise ContractError("admitted capture intent is missing")
    return cast(sqlite3.Row, row)


def _usable(row: sqlite3.Row) -> None:
    if row["invalidated"]:
        raise KnowledgeControlError(
            "REGISTRATION_INVALIDATED",
            "capture registration is permanently invalidated",
        )


def _fence_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ContractError("pending fence limit must be an integer in [1,100]")


def _consumer_capture(
    connection: sqlite3.Connection | None, request: KnowledgeCaptureRequest
) -> sqlite3.Row:
    row = (
        None
        if connection is None
        else connection.execute(
            "SELECT body,registration,consumer,invalidated FROM captures WHERE intent=?",
            (request["intent_id"],),
        ).fetchone()
    )
    if row is None:
        raise KnowledgeControlError(
            "NOT_FOUND", f"capture not found: {request['intent_id']}"
        )
    if row["body"] != _body(request):
        raise KnowledgeControlError(
            "IDEMPOTENCY_CONFLICT", "capture intent body changed"
        )
    return cast(sqlite3.Row, row)


class KnowledgeControl:
    def __init__(
        self, store: RetainedStore, *, schema_digest: str | None = None
    ) -> None:
        from kernelcat_knowledge.query.identity import policy_digest

        self.store = store
        self.schema_digest = schema_digest or policy_digest()

    def denied_live_cards(self, cards: list[dict[str, Any]]) -> set[str]:
        """Apply current exact denials to the same validated card snapshot searched."""
        with self.store.read() as connection:
            if (
                connection is None
                or connection.execute("SELECT 1 FROM denials LIMIT 1").fetchone()
                is None
            ):
                return set()
        # Catalog records are immutable. Resolve their bytes outside the control
        # transaction, then query the current deny authority for these exact inputs.
        # Live file/external selectors are readers, never publication lock paths.
        selection = live_metadata(self.store.root, self.store.binding, cards)
        with self.store.read() as connection:
            if connection is None:
                raise ContractError("capture authority disappeared")
            denied = [
                json.loads(value)
                for value in self._matching_denials(connection, selection)
            ]
        return blocked_cards(
            selection, {(row["kind"], row["object_id"]) for row in denied}
        )

    def control_status(self) -> KnowledgeControlStatus:
        with self.store.read() as connection:
            revision = _revision(connection) if connection is not None else 0
        return {
            "schema_version": "kernelcat.knowledge-control-status/v1",
            "authority_id": self.store.authority,
            "control_revision": revision,
        }

    def consumer_status(
        self, request: KnowledgeCaptureRequest
    ) -> KnowledgeConsumerStatus:
        request = validate_runtime(request, "KnowledgeCaptureRequest")
        with self.store.read() as connection:
            row = _consumer_capture(connection, request)
            # A matched row implies an existing, authority-checked metadata store.
            assert connection is not None
            pending = connection.execute(
                "SELECT COUNT(*) FROM fences WHERE consumer=? AND ack IS NULL",
                (row["consumer"],),
            ).fetchone()[0]
            status: KnowledgeConsumerStatus = {
                "schema_version": "kernelcat.knowledge-consumer-status/v1",
                "registration": json.loads(row["registration"]),
                "control_revision": _revision(connection),
                "invalidated": bool(row["invalidated"]),
                "pending_fence_count": pending,
            }
        return validate_runtime(status, "KnowledgeConsumerStatus")

    def reconcile_consumer(
        self,
        request: KnowledgeCaptureRequest,
        deliver_fence: FenceDelivery,
        limit: int = 100,
    ) -> KnowledgeConsumerStatus:
        request = validate_runtime(request, "KnowledgeCaptureRequest")
        _fence_limit(limit)
        for fence in self._pending_consumer_fences(request, limit):
            try:
                self._deliver_fence(
                    fence["retraction"]["request"]["intent_id"], fence, deliver_fence
                )
            except Exception as exc:
                raise KnowledgeControlError(
                    "FENCE_DELIVERY_FAILED",
                    f"consumer fence delivery incomplete: {request['intent_id']}",
                    status=self.consumer_status(request),
                ) from exc
        return self.consumer_status(request)

    def _pending_consumer_fences(
        self, request: KnowledgeCaptureRequest, limit: int
    ) -> list[KnowledgeFenceRequest]:
        with self.store.read() as connection:
            row = _consumer_capture(connection, request)
            assert connection is not None
            rows = connection.execute(
                "SELECT request FROM fences WHERE consumer=? AND ack IS NULL "
                "ORDER BY intent COLLATE BINARY LIMIT ?",
                (row["consumer"], limit),
            ).fetchall()
        return [json.loads(row["request"]) for row in rows]

    def capture_consumer(
        self, request: KnowledgeCaptureRequest
    ) -> KnowledgeCaptureReceipt:
        request = validate_runtime(request, "KnowledgeCaptureRequest")
        with self.store.read() as connection:
            existing = (
                connection.execute(
                    "SELECT 1 FROM captures WHERE intent=?", (request["intent_id"],)
                ).fetchone()
                if connection is not None
                else None
            )
        if existing is None and self.store.binding is not None:
            self.store.binding.require_sealable()
        row = self._admit_capture(request)
        if row["receipt"] is not None:
            return self._receipt(row["receipt"])
        _usable(row)
        selection = self._selection(request["intent_id"])
        manifest = retain_selection(selection, self.store)
        content = canonical_json(manifest).encode("utf-8")
        view = self._view_reference(manifest, self.store.put_blob(content))
        return self._seal(request["intent_id"], selection, view)

    def _admit_capture(self, request: KnowledgeCaptureRequest) -> sqlite3.Row:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT body,receipt,invalidated FROM captures WHERE intent=?",
                (request["intent_id"],),
            ).fetchone()
            if row is not None:
                if row["body"] != _body(request):
                    raise KnowledgeControlError(
                        "IDEMPOTENCY_CONFLICT", "capture intent body changed"
                    )
                return cast(sqlite3.Row, row)
            _cas(connection, request["expected_control_revision"])
            registration: KnowledgeRegistration = {
                "schema_version": "kernelcat.knowledge-registration/v1",
                "authority_id": self.store.authority,
                "capture_intent_id": request["intent_id"],
                "capture_request_digest": wire_digest(request),
                "registration_epoch": 1,
                "consumer": request["consumer"],
            }
            try:
                binding = {
                    key: value
                    for key, value in request["consumer"].items()
                    if key != "consumer_id"
                }
                connection.execute(
                    "INSERT INTO captures(intent,body,consumer,registration,trial_binding) VALUES (?,?,?,?,?)",
                    (
                        request["intent_id"],
                        _body(request),
                        request["consumer"]["consumer_id"],
                        _body(registration),
                        _body(binding),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise KnowledgeControlError(
                    "CONSUMER_CONFLICT",
                    "consumer already has an immutable registration",
                ) from exc
            return _capture_row(connection, request["intent_id"])

    def _selection(self, intent: str) -> dict[str, Any]:
        with self.store.read() as connection:
            if connection is None:
                raise ContractError("capture authority is missing")
            row = _capture_row(connection, intent)
            _usable(row)
            if row["selection"] is not None:
                return cast(dict[str, Any], json.loads(row["selection"]))
        # Every actual card/source/evidence writer participates in one of these
        # original locks. Fresh owner reads cannot be replaced by loader caches.
        with writer_boundary(self.store.root, self.store.binding):
            with self.store.read() as connection:
                if connection is None:
                    raise ContractError("capture authority is missing")
                row = _capture_row(connection, intent)
                _usable(row)
                if row["selection"] is not None:
                    return cast(dict[str, Any], json.loads(row["selection"]))
            selection = select_metadata(self.store.root, self.store.binding)
            with self.store.read() as connection:
                if connection is None:
                    raise ContractError("capture authority is missing")
                denied = set(self._matching_denials(connection, selection))
            selection = apply_deny_overlay(selection, denied)
            return self._persist_selection(intent, selection)

    def _persist_selection(
        self, intent: str, selection: dict[str, Any]
    ) -> dict[str, Any]:
        with self.store.transaction() as connection:
            row = _capture_row(connection, intent)
            _usable(row)
            if row["selection"] is not None:
                return cast(dict[str, Any], json.loads(row["selection"]))
            connection.execute(
                "UPDATE captures SET selection=? WHERE intent=?",
                (canonical_json(selection), intent),
            )
            connection.executemany(
                "INSERT INTO dependencies VALUES (?,?)",
                [(_body(member), intent) for member in selection["dependencies"]],
            )
            denied = self._selection_denied(connection, selection)
            if denied:
                connection.execute(
                    "UPDATE captures SET invalidated=1 WHERE intent=?", (intent,)
                )
        if denied:
            raise KnowledgeControlError(
                "DEPENDENCY_RETRACTED",
                "selected dependency has a permanent deny decision",
            )
        return selection

    @staticmethod
    def _selection_denied(
        connection: sqlite3.Connection, selection: dict[str, Any]
    ) -> bool:
        return (
            next(KnowledgeControl._matching_denials(connection, selection), None)
            is not None
        )

    @staticmethod
    def _matching_denials(
        connection: sqlite3.Connection, selection: dict[str, Any]
    ) -> Iterator[str]:
        members = (_body(member) for member in selection["dependencies"])
        while page := list(islice(members, 100)):
            placeholders = ",".join("?" for _ in page)
            for row in connection.execute(
                f"SELECT body FROM denials WHERE body IN ({placeholders})", page
            ):
                yield row[0]

    def _view_reference(
        self, manifest: dict[str, Any], digest: str
    ) -> KnowledgeViewReference:
        reference: KnowledgeViewReference = {
            "schema_version": "kernelcat.knowledge-view-reference/v1",
            "authority_id": self.store.authority,
            "view_digest": digest,
            "dependency_digest": wire_digest(manifest["dependencies"]),
            "selection_rule": "active-v1-closure/v1",
            "card_count": sum(
                member["kind"] == "card_revision" for member in manifest["dependencies"]
            ),
            "member_count": len(manifest["dependencies"]),
            "artifact_count": len(manifest["artifacts"]),
            "total_bytes": manifest["total_bytes"],
        }
        return validate_runtime(reference, "KnowledgeViewReference")

    def _seal(
        self, intent: str, selection: dict[str, Any], view: KnowledgeViewReference
    ) -> KnowledgeCaptureReceipt:
        with self.store.transaction() as connection:
            row = _capture_row(connection, intent)
            if row["receipt"] is not None:
                return self._receipt(row["receipt"])
            _usable(row)
            if self._selection_denied(connection, selection):
                raise KnowledgeControlError(
                    "DEPENDENCY_RETRACTED",
                    "selected dependency has a permanent deny decision",
                )
            receipt: KnowledgeCaptureReceipt = {
                "schema_version": "kernelcat.knowledge-capture-receipt/v1",
                "registration": json.loads(row["registration"]),
                "view": view,
                "dependencies": selection["dependencies"],
                "control_revision": _revision(connection),
            }
            receipt = validate_runtime(receipt, "KnowledgeCaptureReceipt")
            connection.execute(
                "INSERT OR IGNORE INTO views(digest,reference) VALUES (?,?)",
                (view["view_digest"], _body(view)),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO view_members VALUES (?,?)",
                [
                    (_body(member), view["view_digest"])
                    for member in selection["dependencies"]
                ],
            )
            connection.execute(
                "UPDATE captures SET receipt=? WHERE intent=?", (_body(receipt), intent)
            )
            return receipt

    @staticmethod
    def _receipt(body: str) -> KnowledgeCaptureReceipt:
        return validate_runtime(
            cast(KnowledgeCaptureReceipt, json.loads(body)), "KnowledgeCaptureReceipt"
        )

    def open_view(self, reference: KnowledgeViewReference) -> RetainedKnowledgeView:
        reference = validate_runtime(reference, "KnowledgeViewReference")
        if reference["authority_id"] != self.store.authority:
            raise KnowledgeControlError(
                "AUTHORITY_CONFLICT", "view belongs to another Knowledge authority"
            )
        with self.store.read() as connection:
            row = (
                None
                if connection is None
                else connection.execute(
                    "SELECT reference FROM views WHERE digest=?",
                    (reference["view_digest"],),
                ).fetchone()
            )
            if row is None or row[0] != _body(reference):
                raise KnowledgeControlError(
                    "VIEW_NOT_FOUND", "retained exact view reference is not registered"
                )
        return RetainedKnowledgeView(
            self.store, reference, schema_digest=self.schema_digest
        )

    def begin_retraction(
        self, request: KnowledgeRetractionRequest
    ) -> KnowledgeRetractionStatus:
        request = validate_runtime(request, "KnowledgeRetractionRequest")
        with self.store.read() as connection:
            replay = self._replay_retraction(connection, request)
            if replay is not None:
                return replay
            _cas(connection, request["expected_control_revision"])
            retained = (
                connection is not None
                and connection.execute(
                    "SELECT 1 FROM dependencies WHERE body=? LIMIT 1",
                    (_body(request["target"]),),
                ).fetchone()
                is not None
            )
        # Persisted membership is immutable and independent of current selectors.
        boundary = (
            nullcontext()
            if retained
            else writer_boundary(self.store.root, self.store.binding)
        )
        with boundary:
            known = retained or known_dependency(
                self.store.root, request["target"], self.store.binding
            )
            with self.store.transaction() as connection:
                replay = self._replay_retraction(connection, request)
                if replay is not None:
                    return replay
                _cas(connection, request["expected_control_revision"])
                if not known:
                    raise KnowledgeControlError(
                        "UNKNOWN_DEPENDENCY",
                        "retraction requires an exact known dependency",
                    )
                return self._admit_retraction(connection, request)

    def _replay_retraction(
        self, connection: sqlite3.Connection | None, request: KnowledgeRetractionRequest
    ) -> KnowledgeRetractionStatus | None:
        if connection is None:
            return None
        existing = connection.execute(
            "SELECT body FROM retractions WHERE intent=?", (request["intent_id"],)
        ).fetchone()
        if existing is None:
            return None
        if existing[0] != _body(request):
            raise KnowledgeControlError(
                "IDEMPOTENCY_CONFLICT", "retraction intent body changed"
            )
        return self._status(connection, request["intent_id"])

    def _admit_retraction(
        self, connection: sqlite3.Connection, request: KnowledgeRetractionRequest
    ) -> KnowledgeRetractionStatus:
        revision = _revision(connection) + 1
        if revision > MAX_SAFE_INTEGER:
            raise KnowledgeControlError(
                "CONTROL_REVISION_LIMIT", "Knowledge control revision is exhausted"
            )
        retraction: KnowledgeRetraction = {
            "schema_version": "kernelcat.knowledge-retraction/v1",
            "authority_id": self.store.authority,
            "request": request,
            "request_digest": wire_digest(request),
            "control_revision": revision,
        }
        connection.execute(
            "INSERT INTO retractions VALUES (?,?,?)",
            (request["intent_id"], _body(request), _body(retraction)),
        )
        connection.execute("UPDATE header SET revision=?", (revision,))
        connection.execute(
            "INSERT OR IGNORE INTO denials VALUES (?)", (_body(request["target"]),)
        )
        connection.execute(
            "UPDATE views SET invalidated=1 WHERE digest IN "
            "(SELECT view FROM view_members WHERE body=?)",
            (_body(request["target"]),),
        )
        self._fix_barrier(connection, retraction)
        return self._status(connection, request["intent_id"])

    @staticmethod
    def _fix_barrier(
        connection: sqlite3.Connection, retraction: KnowledgeRetraction
    ) -> None:
        request = retraction["request"]
        rows = connection.execute(
            "SELECT intent,consumer,registration FROM captures WHERE selection IS NULL OR intent IN "
            "(SELECT capture FROM dependencies WHERE body=?)",
            (_body(request["target"]),),
        )
        for row in rows:
            registration = json.loads(row["registration"])
            request_body = {"registration": registration, "retraction": retraction}
            fence: KnowledgeFenceRequest = {
                "schema_version": "kernelcat.knowledge-fence-request/v1",
                "fence_id": wire_digest(request_body),
                "registration": registration,
                "retraction": retraction,
            }
            connection.execute(
                "INSERT INTO fences(intent,consumer,request) VALUES (?,?,?)",
                (request["intent_id"], row["consumer"], _body(fence)),
            )
            connection.execute(
                "UPDATE captures SET invalidated=1 WHERE intent=?", (row["intent"],)
            )

    @staticmethod
    def _status(
        connection: sqlite3.Connection, intent: str
    ) -> KnowledgeRetractionStatus:
        row = connection.execute(
            "SELECT retraction FROM retractions WHERE intent=?", (intent,)
        ).fetchone()
        if row is None:
            raise KnowledgeControlError("NOT_FOUND", f"retraction not found: {intent}")
        required, fenced = connection.execute(
            "SELECT COUNT(*),COUNT(ack) FROM fences WHERE intent=?", (intent,)
        ).fetchone()
        return {
            "schema_version": "kernelcat.knowledge-retraction-status/v1",
            "retraction": json.loads(row[0]),
            "status": "effective" if required == fenced else "pending",
            "required_consumers": required,
            "fenced_consumers": fenced,
        }

    def retraction_status(self, intent_id: str) -> KnowledgeRetractionStatus:
        validate_runtime(intent_id, "KnowledgeIdentifier")
        with self.store.read() as connection:
            if connection is None:
                raise KnowledgeControlError(
                    "NOT_FOUND", f"retraction not found: {intent_id}"
                )
            return self._status(connection, intent_id)

    def pending_fences(
        self, intent_id: str, cursor: str | None = None, limit: int = 100
    ) -> KnowledgeFencePage:
        validate_runtime(intent_id, "KnowledgeIdentifier")
        _fence_limit(limit)
        if cursor is not None:
            validate_runtime(cursor, "KnowledgeIdentifier")
        with self.store.read() as connection:
            if connection is None:
                raise KnowledgeControlError(
                    "NOT_FOUND", f"retraction not found: {intent_id}"
                )
            status = self._status(connection, intent_id)
            rows = connection.execute(
                "SELECT consumer,request FROM fences WHERE intent=? AND ack IS NULL "
                "AND consumer>? ORDER BY consumer COLLATE BINARY LIMIT ?",
                (intent_id, cursor or "", limit + 1),
            ).fetchall()
            return {
                "schema_version": "kernelcat.knowledge-fence-page/v1",
                "retraction": status["retraction"],
                "requests": [json.loads(row["request"]) for row in rows[:limit]],
                "next_cursor": rows[limit - 1]["consumer"]
                if len(rows) > limit
                else None,
            }

    def reconcile_retraction(
        self, intent_id: str, deliver_fence: FenceDelivery, limit: int = 100
    ) -> KnowledgeRetractionStatus:
        page = self.pending_fences(intent_id, limit=limit)
        for request in page["requests"]:
            try:
                self._deliver_fence(intent_id, request, deliver_fence)
            except Exception as exc:
                status = self.retraction_status(intent_id)
                raise KnowledgeControlError(
                    "FENCE_DELIVERY_FAILED",
                    f"fence delivery incomplete: {intent_id}",
                    status=status,
                ) from exc
        return self.retraction_status(intent_id)

    def _deliver_fence(
        self,
        intent_id: str,
        request: KnowledgeFenceRequest,
        deliver_fence: FenceDelivery,
    ) -> None:
        receipt = deliver_fence(validate_runtime(request, "KnowledgeFenceRequest"))
        receipt = self._check_ack(request, receipt)
        self._persist_ack(intent_id, request, receipt)

    @staticmethod
    def _check_ack(
        request: KnowledgeFenceRequest, receipt: KnowledgeFenceReceipt
    ) -> KnowledgeFenceReceipt:
        receipt = validate_runtime(receipt, "KnowledgeFenceReceipt")
        if (
            receipt["fence_id"] != request["fence_id"]
            or receipt["request_digest"] != wire_digest(request)
            or receipt["registration"] != request["registration"]
        ):
            raise ContractError(
                "trusted Journal fence receipt does not match the exact original request"
            )
        return receipt

    def _persist_ack(
        self,
        intent_id: str,
        request: KnowledgeFenceRequest,
        receipt: KnowledgeFenceReceipt,
    ) -> None:
        consumer = request["registration"]["consumer"]["consumer_id"]
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT request,ack FROM fences WHERE intent=? AND consumer=?",
                (intent_id, consumer),
            ).fetchone()
            if row is None or row["request"] != _body(request):
                raise ContractError(
                    "fence no longer matches its fixed barrier membership"
                )
            if row["ack"] is not None and row["ack"] != _body(receipt):
                raise KnowledgeControlError(
                    "ACK_CONFLICT", "Journal changed an immutable fence receipt"
                )
            connection.execute(
                "UPDATE fences SET ack=? WHERE intent=? AND consumer=?",
                (_body(receipt), intent_id, consumer),
            )
