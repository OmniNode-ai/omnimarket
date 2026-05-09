"""Request model for context bundle generation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_context_bundle_generator_compute.models.model_context_bundle import (
    EnumContextLevel,
)
from omnimarket.nodes.node_context_bundle_generator_compute.models.model_run_context import (
    ModelRunContext,
)
from omnimarket.nodes.node_context_bundle_generator_compute.models.model_task_state import (
    ModelTaskState,
)


class ModelContextBundleRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_state: ModelTaskState
    run_context: ModelRunContext
    requested_level: EnumContextLevel = EnumContextLevel.L2
    # Optional historical fields for L4 bundles — callers supply these from
    # prior run data; the compute node never fetches them.
    historical_summary: str = ""
    prior_attempt_count: int = Field(default=0, ge=0)


__all__ = ["ModelContextBundleRequest"]
