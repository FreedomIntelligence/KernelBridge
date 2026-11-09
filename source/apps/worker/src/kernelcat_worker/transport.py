"""Bounded sequential stdio framing; effects and recovery stay in Executor."""
from __future__ import annotations

from collections.abc import Callable
from typing import BinaryIO

from kernelcat_executor.capabilities.worker import METHODS, WorkerError, decode_request
from kernelcat_executor.contracts.context import validate_context_value
from kernelcat_executor.contracts.loopx_cycle import LOOPX_HOST_METHODS
from kernelcat_executor.contracts.models import Json
from kernelcat_executor.contracts.runtime import RUNTIME_METHODS

from kernelcat_worker.payloads import PAYLOAD_METHODS, PayloadSession


def _write_frame(writer: BinaryIO, response: bytes) -> None:
    outgoing = memoryview(response + b"\n")
    while outgoing:
        count = writer.write(outgoing)
        if count <= 0:
            raise WorkerError("stdio response pipe stopped accepting bytes; inspect receipts")
        outgoing = outgoing[count:]
    writer.flush()


def serve_stream(reader: BinaryIO, writer: BinaryIO,
                 handle: Callable[[str, Json], Json], *, max_frame_bytes: int,
                 runtime: bool = False, loopx: bool = False) -> Json:
    """Serve exactly one RPC session, without redispatch after any I/O failure."""
    if not 4096 <= max_frame_bytes <= 16777216:
        raise WorkerError("invalid stdio frame limit")
    payloads = PayloadSession(max_frame_bytes)
    methods = METHODS | PAYLOAD_METHODS
    if runtime:
        methods |= RUNTIME_METHODS
    if loopx:
        methods |= LOOPX_HOST_METHODS
    terminal_method = "loopx_finish" if loopx else "complete"
    completion = None
    try:
        request_id = 1
        while True:
            raw = _read_frame(reader, max_frame_bytes)
            method, params = decode_request(raw, request_id, methods=methods)
            validate_context_value(params, "RPC request")
            result = payloads.handle(method, params, request_id, handle)
            validate_context_value(result, "RPC response")
            response = payloads.response(request_id, result)
            if len(response) > max_frame_bytes:
                raise WorkerError("stdio response exceeds frame limit; receipts remain authoritative")
            _write_frame(writer, response)
            request_id += 1
            if method == terminal_method:
                completion = result
            if completion is not None and payloads.outgoing is None:
                return completion
    finally:
        payloads.close()


def _read_frame(reader: BinaryIO, max_frame_bytes: int) -> bytes:
    raw = reader.readline(max_frame_bytes + 2)
    if not raw:
        raise WorkerError("stdio EOF before protocol completion; inspect receipts before resuming")
    if len(raw) > max_frame_bytes + 1:
        raise WorkerError("stdio request exceeds frame limit")
    if not raw.endswith(b"\n"):
        raise WorkerError("stdio request is an unterminated frame")
    return raw[:-1]
