"""Private, action-bound provider observations; never replay network effects.

These are assembled response values, not original HTTP/SSE wire bytes. Only
compact references and bounded parser diagnostics belong in model receipts.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import re

from pydantic import ValidationError

from kernelcat_executor.capabilities.runtime_artifacts import RuntimeArtifacts
from kernelcat_executor.contracts.models import canonical

_active = ContextVar("kernelbridge_provider_evidence", default=None)
_SENSITIVE = {"authorization", "proxy-authorization", "x-api-key", "api-key",
              "access-token", "refresh-token", "client-secret", "password"}
_JSON_ESCAPE = re.compile(r'\\u[dD][89aAbB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}|\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])')
_DECODE_LAYERS = 8
_DECODE_CHARACTERS = 8 * 1024 * 1024


class _RedactionLimit(ValueError):
    pass


def _encoded_secret(value, secret, budget):
    """Inspect JSON escapes even in malformed arguments, without retaining them.

    Each pass can expose another serialized string layer. Ambiguous content at
    the limit is unavailable evidence, never a supposedly unredacted response.
    """
    for _ in range(_DECODE_LAYERS):
        budget[0] -= len(value)
        if budget[0] < 0:
            raise _RedactionLimit()
        if secret and secret in value:
            return True
        decoded = _JSON_ESCAPE.sub(lambda match: json.loads('"' + match[0] + '"'), value)
        if decoded == value:
            return False
        value = decoded
    if secret and secret in value:
        return True
    if _JSON_ESCAPE.search(value):
        raise _RedactionLimit()
    return False


def _scrub(value, secret, changes, *, layers=0, budget=None):
    budget = [_DECODE_CHARACTERS] if budget is None else budget
    if isinstance(value, str):
        if secret and secret in value:
            changes.append("configured_credential")
            value = value.replace(secret, "[REDACTED]")
        if _encoded_secret(value, secret, budget):
            changes.append("encoded_configured_credential")
            return "[REDACTED]"
        # Tools can carry another JSON object inside arguments. Inspect its
        # credential fields too, preserving the original string when unchanged.
        if value.lstrip().startswith(("{", "[", '"')):
            if layers >= _DECODE_LAYERS:
                raise _RedactionLimit()
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return value
            except RecursionError:
                raise _RedactionLimit() from None
            before = len(changes)
            scrubbed = _scrub(decoded, secret, changes, layers=layers + 1, budget=budget)
            if len(changes) != before:
                return json.dumps(scrubbed, ensure_ascii=True, allow_nan=True)
        return value
    if isinstance(value, list):
        return [_scrub(item, secret, changes, layers=layers, budget=budget) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("_", "-")
            if normalized in _SENSITIVE:
                changes.append("credential_field")
                item = "[REDACTED]"
            result[_scrub(key, secret, changes, layers=layers, budget=budget)] = _scrub(
                item, secret, changes, layers=layers, budget=budget)
        return result
    return value


def parse_diagnostic(error, *, stage, secret=""):
    """Keep the cause without Pydantic's raw input/context or JSONDecodeError.doc."""
    if isinstance(error, ValidationError):
        problems = error.errors(include_input=False, include_context=False, include_url=False)
        reason = "; ".join(str(item.get("loc")) + ": " + item["type"] + " " + item["msg"]
                           for item in problems[:4])
    elif isinstance(error, json.JSONDecodeError):
        reason = f"{error.msg} at line {error.lineno} column {error.colno} (char {error.pos})"
    else:
        reason = str(error)
    try:
        reason = _scrub(reason, secret, [])
    except _RedactionLimit:
        reason = "Diagnostic omitted: safe redaction exceeded its decoding limit"
    except Exception:
        # Diagnostics are ancillary too: malformed/deep input must never replace
        # the original parser failure, its usage, or the owner's retry decision.
        reason = "Diagnostic omitted: safe redaction could not be completed"
    return {"stage": stage, "exception_type": type(error).__name__,
            "reason": "".join(char if char.isprintable() else " " for char in reason)[:512]}


class ProviderEvidence:
    def __init__(self, store, admitted, request):
        # RuntimeStore is a view of its original durable owner, not a new ledger.
        path = getattr(store, "path", None) or getattr(getattr(store, "owner", None), "path", None)
        action = admitted["action"]
        self.artifacts = None
        self.reference = {"status": "unavailable", "reason": "no_assembled_response",
                          "wire_complete": False}
        if path:
            try:
                self.artifacts = RuntimeArtifacts(Path(path).parent / "model-responses" /
                    hashlib.sha256(action["id"].encode()).hexdigest())
            except Exception as error:
                self.reference = {"status": "unavailable", "reason": "artifact_capture_failed",
                                  "error_type": type(error).__name__, "wire_complete": False}
        context = admitted.get("application_context", {})
        self.binding = {"action_id": action["id"], "task_id": action["task"],
                        "request_digest": request.fingerprint(), "provider": request.provider,
                        "context_digest": action["arguments"].get("context_digest"),
                        "source": {key: context[key] for key in (
                            "source_sha256", "evaluator_sha256", "task_version", "trial_id", "goal_id",
                            "pi_session_id", "entry_id", "child_id") if key in context},
                        "application_session_id": admitted.get("application_session_id"),
                        "runtime_binding": admitted.get("binding")}

    def capture(self, data, *, scope, secret):
        if self.artifacts is None:
            if self.reference["reason"] == "no_assembled_response":
                self.reference = {"status": "unavailable", "reason": "store_has_no_artifact_path",
                                  "wire_complete": False}
            return
        try:
            changes = []
            value = _scrub(data, secret, changes)
            # Preserve even a nonfinite value rejected by the later turn validator.
            # The outer record stays strict JSON; data_json is a replayable string.
            encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=True)
            record = {"schema_version": "kernelbridge.provider-response/v1", **self.binding,
                      "capture_scope": scope, "wire_complete": False,
                      "redacted": bool(changes), "redactions": sorted(set(changes)),
                      "data_encoding": "python-json", "data_json": encoded}
            reference = self.artifacts.put(canonical(record).encode(), "provider-response")
            self.reference = {"status": "recorded", "capture_scope": scope,
                              "wire_complete": False, "redacted": bool(changes), **reference}
        except _RedactionLimit:
            self.reference = {"status": "unavailable", "reason": "redaction_limit",
                              "wire_complete": False}
        except Exception as error:
            # Evidence I/O cannot change a known provider failure into unknown, or
            # obscure usage that has already been observed by the adapter.
            self.reference = {"status": "unavailable", "reason": "artifact_capture_failed",
                              "error_type": type(error).__name__, "wire_complete": False}


@contextmanager
def capture_provider_evidence(evidence):
    token = _active.set(evidence)
    try:
        yield
    finally:
        _active.reset(token)


def observe_response(data, *, scope="assembled_provider_response", secret=""):
    evidence = _active.get()
    if evidence is not None:
        evidence.capture(data, scope=scope, secret=secret)


def failure_evidence():
    evidence = _active.get()
    return {"provider_response": dict(evidence.reference)} if evidence is not None else {}
