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
    # ISO date (YYYY-MM-DD) for the ADR **Date** field. Derived from
    # extraction.provenance.timestamp when empty so output is fully
    # determined by inputs and never reads the wall clock.
    adr_date: str = ""


__all__ = ["ModelADRGenerationRequest"]
