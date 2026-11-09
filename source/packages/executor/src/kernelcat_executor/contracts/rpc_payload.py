"""Validate ephemeral transport requests against the generated wire authority."""
from __future__ import annotations

from typing import cast

from jsonschema import Draft202012Validator

from kernelcat_executor.contracts.json_snapshot import validated_snapshot
from kernelcat_executor.generated.rpc_payload import (
    SCHEMA,
    PayloadBeginRequest,
    PayloadCallRequest,
    PayloadReadRequest,
    PayloadWriteRequest,
)


def _validated(value: object, name: str) -> object:
    validator = Draft202012Validator({**SCHEMA, "$ref": "#/$defs/" + name})
    return validated_snapshot(value, validator, name)


def validate_payload_begin(value: object) -> PayloadBeginRequest:
    return cast(PayloadBeginRequest, _validated(value, "PayloadBeginRequest"))


def validate_payload_read(value: object) -> PayloadReadRequest:
    return cast(PayloadReadRequest, _validated(value, "PayloadReadRequest"))


def validate_payload_write(value: object) -> PayloadWriteRequest:
    return cast(PayloadWriteRequest, _validated(value, "PayloadWriteRequest"))


def validate_payload_call(value: object) -> PayloadCallRequest:
    return cast(PayloadCallRequest, _validated(value, "PayloadCallRequest"))
