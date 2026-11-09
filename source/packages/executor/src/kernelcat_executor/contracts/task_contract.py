"""Finite TaskContract wire validation; semantic scoring belongs to TS/Node."""
from __future__ import annotations

import hashlib
import json
from typing import NoReturn, cast

from jsonschema import Draft202012Validator

from kernelcat_executor.contracts.json_snapshot import validated_snapshot
from kernelcat_executor.generated.task_contract import (
    SCHEMA,
    ScoringResultSet,
    TaskContract,
    TaskContractSource,
)

_VALIDATOR = Draft202012Validator(SCHEMA)
_SCORING_VALIDATOR = Draft202012Validator({**SCHEMA, "$ref": "#/$defs/ScoringResultSet"})


def _reject_json_constant(text: str) -> NoReturn:
    raise ValueError(f"Non-JSON numeric constant: {text}")


def validate_task_contract(value: object) -> TaskContract:
    """Return a normalized validated JSON snapshot without touching service state."""
    return cast(TaskContract, validated_snapshot(value, _VALIDATOR, "TaskContract"))


def validate_scoring_result_set(value: object) -> ScoringResultSet:
    """Validate projection syntax; no observation is qualified or published here."""
    return cast(ScoringResultSet, validated_snapshot(value, _SCORING_VALIDATOR, "ScoringResultSet"))


def load_task_contract(source: bytes | bytearray) -> TaskContractSource:
    """Decode and hash one source snapshot, using Node-compatible number decoding."""
    try:
        raw = bytes(source)
        # Match JSON.parse: decode numbers as doubles and retain the last key.
        # Validate finiteness on the surviving tree, not discarded duplicate values.
        value = json.loads(
            raw.decode("utf-8"),
            parse_int=float,
            parse_float=float,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"Cannot decode TaskContract UTF-8 JSON source: {error}") from error
    contract = validate_task_contract(value)
    return {"source_digest": "sha256:" + hashlib.sha256(raw).hexdigest(), "contract": contract}
