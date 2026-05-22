# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelTestGenerationRequest — input for node_test_generator."""

from __future__ import annotations

from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from pydantic import BaseModel, ConfigDict, Field


class ModelTestGenerationRequest(BaseModel):
    """Command payload for test generation from a ticket contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: ModelTicketContract = Field(
        ...,
        description="The ticket contract to generate tests for.",
    )
    generator_version: str = Field(
        default="1.0.0",
        description="Version of the test generator (semver). Included in determinism key.",
    )
    generation_profile_hash: str = Field(
        default="default",
        description="Hash or label identifying the generation profile configuration.",
    )
    correlation_id: str = Field(
        default="",
        description="Correlation ID linking this request to the triggering event.",
    )


__all__: list[str] = ["ModelTestGenerationRequest"]
