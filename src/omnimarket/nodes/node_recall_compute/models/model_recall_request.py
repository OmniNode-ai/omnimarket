"""Request model for node_recall_compute."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelRecallFilters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str | None = None
    task_type: str | None = None


class ModelRecallRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    scope: str = "all"
    filters: ModelRecallFilters | None = None
    max_results: int = 5


__all__ = ["ModelRecallFilters", "ModelRecallRequest"]
