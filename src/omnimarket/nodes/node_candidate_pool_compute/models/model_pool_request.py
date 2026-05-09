"""Request model for candidate pool scoring."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCandidatePoolRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: list[str]
    target_schema: dict[str, object]
    max_loc: int = Field(ge=0)
    min_candidates: int = Field(default=1, ge=1)
    run_id: str = ""


__all__ = ["ModelCandidatePoolRequest"]
