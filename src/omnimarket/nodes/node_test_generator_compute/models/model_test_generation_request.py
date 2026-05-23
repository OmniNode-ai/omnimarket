"""Request model for deterministic ticket-contract test generation."""

from __future__ import annotations

from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from pydantic import BaseModel, ConfigDict, Field


class ModelTestGenerationRequest(BaseModel):
    """Inputs for pure generated-test artifact creation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: ModelTicketContract
    generator_version: str = Field(default="1.0.0", min_length=1)
    generation_profile_hash: str = Field(default="profile_default", min_length=1)


__all__ = ["ModelTestGenerationRequest"]
