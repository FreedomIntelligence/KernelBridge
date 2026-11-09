"""Immutable, reconstructable model requests without authentication material."""
from __future__ import annotations

import json
from typing import Literal, cast

from pydantic import Field, field_validator, model_validator

from kernelcat_executor.contracts.models import Contract, Json, canonical, digest
from kernelcat_executor.contracts.providers import validate_endpoint


class ModelRequest(Contract):
    """JSON strings avoid the shallow mutability of dictionaries in frozen models."""

    schema_version: Literal["kernelagent.model-request/v1"] = "kernelagent.model-request/v1"
    provider: Literal["openai", "anthropic"]
    endpoint: str
    body_json: str
    public_headers_json: str = '{"content-type":"application/json"}'
    timeout: float = Field(gt=0, le=3600, default=120.0)
    allow_insecure_http: bool = False

    @model_validator(mode="after")
    def valid_transport(self) -> ModelRequest:
        validate_endpoint(self.endpoint, self.allow_insecure_http)
        return self

    @field_validator("body_json", "public_headers_json")
    @classmethod
    def canonical_object(cls, value: str) -> str:
        decoded = json.loads(value)
        if not isinstance(decoded, dict) or canonical(decoded) != value:
            raise ValueError("request components must be finite canonical JSON objects")
        return value

    @field_validator("public_headers_json")
    @classmethod
    def public_headers_only(cls, value: str) -> str:
        headers = json.loads(value)
        if not all(k in {"content-type", "anthropic-version"} and isinstance(v, str)
                   for k, v in headers.items()):
            raise ValueError("only public protocol headers may be recorded")
        if headers.get("content-type") != "application/json":
            raise ValueError("JSON content type is required")
        return value

    def payload(self) -> Json:
        return cast(Json, json.loads(self.body_json))

    def public_headers(self) -> dict[str, str]:
        return cast(dict[str, str], json.loads(self.public_headers_json))

    def fingerprint(self) -> str:
        return digest(self)
