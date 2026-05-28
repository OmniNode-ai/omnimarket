"""Result models for node_recall_compute."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumRecallConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class ModelKnowledgeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    content: str
    rank: int = 0
    similarity: float | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class ModelRecallResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    results: tuple[ModelKnowledgeResult, ...] = ()
    sources: tuple[str, ...] = ()
    confidence: EnumRecallConfidence = EnumRecallConfidence.NONE
    partial: bool = False
    error: str | None = None


__all__ = ["EnumRecallConfidence", "ModelKnowledgeResult", "ModelRecallResult"]
