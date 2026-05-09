"""Result model for output schema registry lookups."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class EnumSchemaRegistryStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class ModelSchemaRegistryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumSchemaRegistryStatus
    schema_key: str
    json_schema: dict[str, Any] | None = None
    error: str | None = None


__all__ = ["EnumSchemaRegistryStatus", "ModelSchemaRegistryResult"]
