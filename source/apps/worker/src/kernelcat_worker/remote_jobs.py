"""Finite remote execution after original Gateway admission; no retry authority."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn, Protocol, cast

from kernelcat_executor.contracts.models import Json

from kernelcat_worker import remote_job_runner

JobStatus = Literal["running", "completed", "failed", "unknown"]


class Gateway(Protocol):
    """Injected finite transport, including the user's gateway_ssh helper."""

    def run(self, argv: list[str], *, cwd: str | None = None, stdin: bytes = b"",
            timeout: float = 120) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class RemoteJob:
    identity: str
    request_sha256: str
    directory: str


@dataclass(frozen=True)
class JobResult:
    status: JobStatus
    handle: RemoteJob
    result: Json | None = None
    reason: str | None = None


class RemoteJobError(RuntimeError):
    def __init__(self, handle: RemoteJob, reason: str) -> None:
        self.handle = handle
        self.reason = reason
        super().__init__(f"remote job {handle.identity}: {reason}")


class RemoteJobConflict(RemoteJobError):
    """The original identity is already bound to different input."""


class RemoteJobUncertain(RemoteJobError):
    """Launch may have happened. Reconcile handle; never redispatch."""


class RemoteJobProtocolError(RemoteJobError):
    """Retained state or the observation response failed validation."""


class RemoteJobs:
    """One create-only directory per admitted action; no budget or agent loop.

    Submit touches only supplied input bytes. Completed observation rechecks bounded
    inputs, metadata and result bytes, without logs, history scans or new directories.
    """

    def __init__(self, gateway: Gateway, root: str, python: str) -> None:
        self.gateway = gateway
        self.root = remote_job_runner.absolute_path(root)
        self.python = remote_job_runner.absolute_path(python)

    def submit(self, identity: str, request: Json, files: Mapping[str, bytes],
               argv: Sequence[str]) -> RemoteJob:
        source = Path(remote_job_runner.__file__).read_bytes()
        submission = remote_job_runner.prepare_submission(
            identity, request, files, argv, self.python, source)
        handle = RemoteJob(identity, submission["manifest"]["request_sha256"],
                           self.root + "/" + hashlib.sha256(identity.encode()).hexdigest())
        response = self._call("submit", handle, remote_job_runner.encode_json(submission))
        self._check_response(handle, response)
        if response != {"ok": True, "handle": self._handle_json(handle)}:
            raise RemoteJobUncertain(handle, "invalid submit acknowledgement; inspect original handle")
        return handle

    def status(self, handle: RemoteJob) -> JobResult:
        self._check_handle(handle)
        try:
            response = self._call("status", handle)
        except RemoteJobUncertain as error:
            return JobResult("unknown", handle, reason=error.reason)
        self._check_response(handle, response)
        expected = {"ok", "handle", "status", "result", "reason"}
        if set(response) != expected or response["handle"] != self._handle_json(handle):
            raise RemoteJobProtocolError(handle, "observation does not bind the original handle")
        state, result, reason = response["status"], response["result"], response["reason"]
        if not isinstance(state, str) or state not in {"running", "completed", "failed", "unknown"}:
            raise RemoteJobProtocolError(handle, "observation has an invalid status")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 2048):
            raise RemoteJobProtocolError(handle, "observation has an invalid reason")
        if state == "completed":
            if not isinstance(result, dict) or reason is not None:
                raise RemoteJobProtocolError(handle, "completion requires a JSON object and no failure")
        elif result is not None or (state != "running" and not reason):
            raise RemoteJobProtocolError(handle, "non-completion cannot contain a result")
        return JobResult(cast(JobStatus, state), handle, result, reason)

    def collect(self, handle: RemoteJob) -> JobResult:
        """Observe the original command without waiting or launching another."""
        return self.status(handle)

    def _call(self, operation: str, handle: RemoteJob, stdin: bytes = b"") -> Json:
        source = Path(remote_job_runner.__file__).read_text()
        argv = [self.python, "-I", "-B", "-c", source, operation, self.root,
                handle.directory, handle.identity, handle.request_sha256]
        try:
            response = self.gateway.run(argv, stdin=stdin, timeout=120)
        except Exception as error:
            raise RemoteJobUncertain(handle, "transport did not acknowledge original " + operation) from error
        if not isinstance(response, Mapping):
            self._invalid_response(operation, handle, "malformed gateway response")
        returncode, stdout, stderr = response.get("returncode"), response.get("stdout"), response.get("stderr")
        if type(returncode) is not int or not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
            self._invalid_response(operation, handle, "malformed gateway response")
        if returncode != 0:
            raise RemoteJobUncertain(handle, "remote observer exited without a valid acknowledgement")
        if len(stdout) > remote_job_runner.MAX_RESPONSE_BYTES:
            self._invalid_response(operation, handle, "remote response exceeds its bound")
        try:
            value = remote_job_runner.decode_json(stdout)
        except (ValueError, UnicodeError) as error:
            self._invalid_response(operation, handle, "remote response is not valid JSON", error)
        if not isinstance(value, dict):
            self._invalid_response(operation, handle, "remote response must be a JSON object")
        return value

    @staticmethod
    def _invalid_response(operation: str, handle: RemoteJob, reason: str,
                          cause: Exception | None = None) -> NoReturn:
        error_type = RemoteJobUncertain if operation == "submit" else RemoteJobProtocolError
        raise error_type(handle, reason) from cause

    @staticmethod
    def _handle_json(handle: RemoteJob) -> dict[str, str]:
        return {"identity": handle.identity, "request_sha256": handle.request_sha256,
                "directory": handle.directory}

    def _check_handle(self, handle: RemoteJob) -> None:
        if (not isinstance(handle.identity, str) or not handle.identity or "\0" in handle.identity
                or len(handle.identity.encode()) > 2048
                or not isinstance(handle.request_sha256, str)
                or not isinstance(handle.directory, str)):
            raise RemoteJobProtocolError(handle, "handle fields require nonempty strings")
        expected = self.root + "/" + hashlib.sha256(handle.identity.encode()).hexdigest()
        if (handle.directory != expected or len(handle.request_sha256) != 64
                or any(character not in "0123456789abcdef" for character in handle.request_sha256)):
            raise RemoteJobProtocolError(handle, "handle does not belong to this remote job root")

    @staticmethod
    def _check_response(handle: RemoteJob, response: Json) -> None:
        if response.get("ok") is True:
            return
        if (set(response) != {"ok", "kind", "reason"} or response.get("ok") is not False
                or not isinstance(response.get("reason"), str)):
            raise RemoteJobProtocolError(handle, "malformed remote error acknowledgement")
        kind = response["kind"]
        if not isinstance(kind, str):
            raise RemoteJobProtocolError(handle, "remote error kind requires a string")
        error_type = {"conflict": RemoteJobConflict, "uncertain": RemoteJobUncertain,
                      "invalid": RemoteJobProtocolError}.get(kind)
        if error_type is None:
            raise RemoteJobProtocolError(handle, "unknown remote error acknowledgement")
        raise error_type(handle, response["reason"])
