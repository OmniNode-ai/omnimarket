"""Result model for schema repair prompt construction."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumRepairStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class ModelSchemaRepairResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumRepairStatus
    repair_prompt: str
    error_summary: str
    repairable: bool
    run_id: str
    error: str | None = None


__all__ = ["EnumRepairStatus", "ModelSchemaRepairResult"]
