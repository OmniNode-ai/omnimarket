"""Request model for deterministic golden-chain generation."""

from __future__ import annotations

from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from pydantic import BaseModel, ConfigDict, Field


class ModelGoldenChainGenerationRequest(BaseModel):
    """Inputs for deriving an expected chain from contract truth."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: ModelTicketContract
    generated_test_hash: str | None = None
    generator_version: str = Field(default="1.0.0", min_length=1)
    template_hash: str | None = None


__all__ = ["ModelGoldenChainGenerationRequest"]
