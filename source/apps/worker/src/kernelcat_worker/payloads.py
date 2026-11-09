"""Session-local byte paging; execution and durable effect identity stay in Host."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO
from uuid import uuid4

from kernelcat_executor.capabilities.worker import PROTOCOL, WorkerError, decode_object
from kernelcat_executor.contracts.context import validate_context_value
from kernelcat_executor.contracts.models import Json
from kernelcat_executor.contracts.rpc_payload import (
    validate_payload_begin,
    validate_payload_call,
    validate_payload_read,
    validate_payload_write,
)
from kernelcat_executor.generated.rpc_payload import (
    PayloadBeginRequest,
    PayloadCallRequest,
    PayloadMethod,
    PayloadReadRequest,
    PayloadReadResult,
    PayloadRef,
    PayloadReference,
    PayloadWriteRequest,
    PayloadWriteResult,
)

PAYLOAD_METHODS = frozenset({"payload_begin", "payload_write", "payload_read", "payload_call"})
PAGE_BYTES = 65536
_ENCODER = json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True,
                            separators=(",", ":"))


def response_bytes(identity: int, result: object) -> bytes:
    return json.dumps({"protocol": PROTOCOL, "id": identity, "result": result},
                      ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


@dataclass
class BytePayload:
    reference: PayloadRef
    stream: IO[bytes]
    offset: int = 0
    method: PayloadMethod = "model"


class PayloadSession:
    """At most one outgoing spool and one uninvoked upload per stdio session."""
    def __init__(self, max_frame_bytes: int) -> None:
        self.max_frame_bytes = max_frame_bytes
        self.outgoing: BytePayload | None = None
        self.incoming: BytePayload | None = None

    def close(self) -> None:
        for payload in (self.outgoing, self.incoming):
            if payload is not None:
                payload.stream.close()
        self.outgoing, self.incoming = None, None

    @staticmethod
    def _reference(payload: BytePayload) -> PayloadReference:
        return {"transport": "kernelcat.rpc-payload/v1", "ref": payload.reference.copy()}

    def response(self, identity: int, result: Json) -> bytes:
        """Spool iteratively; retain large results until all pages are delivered."""
        stream = tempfile.SpooledTemporaryFile[bytes](max_size=self.max_frame_bytes, mode="w+b")
        retained = False
        try:
            hasher = hashlib.sha256()
            size = 0
            for fragment in _ENCODER.iterencode(result):
                raw = fragment.encode("utf-8")
                stream.write(raw)
                hasher.update(raw)
                size += len(raw)
            overhead = len(response_bytes(identity, {})) - 2
            if size + overhead <= self.max_frame_bytes:
                return response_bytes(identity, result)
            if self.outgoing is not None:
                raise WorkerError("payload page exceeds its bounded response frame")
            stream.seek(0)
            reference: PayloadRef = {"id": uuid4().hex, "bytes": size, "sha256": hasher.hexdigest()}
            self.outgoing = BytePayload(reference, stream)
            retained = True
            return response_bytes(identity, self._reference(self.outgoing))
        finally:
            if not retained:
                stream.close()

    def handle(self, method: str, params: Json, identity: int,
               execute: Callable[[str, Json], Json]) -> Json:
        if self.outgoing is not None and method != "payload_read":
            raise WorkerError("finish reading the current response payload before another request")
        if self.incoming is not None and method not in {"payload_write", "payload_call"}:
            raise WorkerError("finish the current upload before another request")
        if method == "payload_read":
            return dict(self._read(validate_payload_read(params), identity))
        if method == "payload_begin":
            return dict(self._begin(validate_payload_begin(params)))
        if method == "payload_write":
            return dict(self._write(validate_payload_write(params)))
        if method == "payload_call":
            return self._call(validate_payload_call(params), execute)
        return execute(method, params)

    def _begin(self, request: PayloadBeginRequest) -> PayloadReference:
        method = request.get("method", "model")
        if method == "model":
            limit = 8 * 1024 * 1024 + 4096
        elif method in {"rsi_record", "rsi_adapter", "rsi_apply"}:
            limit = 2 * 1024 * 1024
        elif method in {"prepare_run", "freeze_run", "snapshot_program", "adopt_candidate", "stop_run", "knowledge_fence", "prepare_trial", "freeze_trial", "knowledge_sync"}:
            limit = 16 * 1024 * 1024 + 4096
        else:
            limit = 4198400
        if request["bytes"] > limit:
            raise WorkerError("request upload exceeds its method-bound byte limit")
        reference: PayloadRef = {"id": uuid4().hex, "bytes": request["bytes"],
                                 "sha256": request["sha256"]}
        self.incoming = BytePayload(reference, tempfile.TemporaryFile(mode="w+b"), method=request.get("method", "model"))
        return self._reference(self.incoming)

    def _read(self, request: PayloadReadRequest, identity: int) -> PayloadReadResult:
        payload = self.outgoing
        if payload is None or request["ref"] != payload.reference:
            raise WorkerError("response payload reference has no live session spool")
        if request["offset"] != payload.offset:
            raise WorkerError("response payload offset is not the next unread byte")
        empty: PayloadReadResult = {"ref": payload.reference.copy(), "offset": payload.offset,
                                   "data": "", "final": False}
        budget = self.max_frame_bytes - len(response_bytes(identity, empty))
        limit = min(request["max_bytes"], PAGE_BYTES, (budget // 4) * 3)
        if limit <= 0:
            raise WorkerError("response frame cannot contain payload page metadata")
        raw = payload.stream.read(limit)
        if not raw:
            raise WorkerError("response spool ended before its declared byte count")
        result: PayloadReadResult = {**empty, "data": base64.b64encode(raw).decode("ascii"),
                                    "final": payload.offset + len(raw) == payload.reference["bytes"]}
        payload.offset += len(raw)
        if result["final"]:
            payload.stream.close()
            self.outgoing = None
        return result

    def _write(self, request: PayloadWriteRequest) -> PayloadWriteResult:
        payload = self.incoming
        if payload is None or request["ref"] != payload.reference:
            raise WorkerError("upload reference has no live session payload")
        if request["offset"] != payload.offset:
            raise WorkerError("upload offset is not the next unwritten byte")
        try:
            raw = base64.b64decode(request["data"], validate=True)
        except binascii.Error as error:
            raise WorkerError("upload page is not valid base64") from error
        if base64.b64encode(raw).decode("ascii") != request["data"]:
            raise WorkerError("upload page is not canonical base64")
        if payload.offset + len(raw) > payload.reference["bytes"]:
            raise WorkerError("upload page exceeds its declared byte count")
        payload.stream.write(raw)
        payload.offset += len(raw)
        return {"ref": payload.reference.copy(), "next_offset": payload.offset}

    def _call(self, request: PayloadCallRequest, execute: Callable[[str, Json], Json]) -> Json:
        payload = self.incoming
        if payload is None or request["ref"] != payload.reference:
            raise WorkerError("request upload reference has no live session payload")
        # Consume before parsing or dispatch. No response loss authorizes a second call.
        self.incoming = None
        try:
            if request["method"] != payload.method:
                raise WorkerError("upload method differs from its bound request")
            if payload.offset != payload.reference["bytes"]:
                raise WorkerError("request upload is incomplete")
            payload.stream.seek(0)
            digest = hashlib.sha256()
            for page in iter(lambda: payload.stream.read(PAGE_BYTES), b""):
                digest.update(page)
            if digest.hexdigest() != payload.reference["sha256"]:
                raise WorkerError("request upload digest does not match its admitted bytes")
            payload.stream.seek(0)
            params = decode_object(payload.stream.read())
            validate_context_value(params, "uploaded request")
            return execute(request["method"], params)
        finally:
            payload.stream.close()
