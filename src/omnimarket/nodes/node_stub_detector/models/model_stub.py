"""A single detected method stub."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelStub(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    method_name: str
    signature: str
    marker: str


__all__ = ["ModelStub"]
