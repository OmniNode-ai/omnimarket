"""Request model for schema repair prompt construction."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelSchemaRepairRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    malformed_output: str
    validation_errors: list[dict[str, object]]
    target_schema: dict[str, object]
    original_prompt: str
    run_id: str = ""


__all__ = ["ModelSchemaRepairRequest"]
