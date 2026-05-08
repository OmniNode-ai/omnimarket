"""Request model for ADR draft generation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_adr_draft_generation_compute.models.model_decision_extraction import (
    ModelDecisionExtraction,
)


class ModelADRGenerationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    extraction: ModelDecisionExtraction
    run_id: str = ""


__all__ = ["ModelADRGenerationRequest"]
