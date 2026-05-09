"""Request model for output schema registry lookups."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelSchemaRegistryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_key: str
    run_id: str = ""


__all__ = ["ModelSchemaRegistryRequest"]
