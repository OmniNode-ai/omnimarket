"""Result models for candidate pool scoring."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumPoolStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class ModelScoredCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    original_index: int
    schema_valid: bool
    loc: int
    within_budget: bool
    fitness_score: float


class ModelCandidatePoolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumPoolStatus
    run_id: str
    ranked_candidates: list[ModelScoredCandidate]
    best_candidate_index: int
    all_valid: bool
    summary: str
    error: str | None = None


__all__ = ["EnumPoolStatus", "ModelCandidatePoolResult", "ModelScoredCandidate"]
