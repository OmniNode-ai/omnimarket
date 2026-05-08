"""Result model for ADR draft generation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumGenerationStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class ModelADRGenerationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumGenerationStatus
    extraction_id: str
    run_id: str
    markdown: str
    error: str | None = None


__all__ = ["EnumGenerationStatus", "ModelADRGenerationResult"]
