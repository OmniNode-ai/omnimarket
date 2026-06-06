"""Request model for node_recall_compute."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

RECALL_SCOPES = ("learnings", "architecture", "antipatterns", "all")


class ModelRecallFilters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str | None = None
    task_type: str | None = None


class ModelRecallRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(..., min_length=1)
    scope: str = "all"
    filters: ModelRecallFilters | None = None
    max_results: int = Field(default=5, ge=1, le=50)

    @field_validator("query", "scope")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, value: str) -> str:
        if value not in RECALL_SCOPES:
            raise ValueError("scope must be one of " + ", ".join(RECALL_SCOPES))
        return value


__all__ = ["RECALL_SCOPES", "ModelRecallFilters", "ModelRecallRequest"]
