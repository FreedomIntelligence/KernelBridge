"""Shared immutable, closed-world data contract; no I/O or runtime hooks."""
from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
