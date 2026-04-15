# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output model for node_code_enrichment_effect.

Schema must match the dict emitted by HandlerCodeEnrichmentEffect.handle().
[OMN-5657, OMN-5664]
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCodeEnrichmentResult(BaseModel):
    """Result of a code entity LLM enrichment batch.

    Published to onex.evt.omniintelligence.code-enriched.v1.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str = Field(
        ..., min_length=1, description="Propagated from handler input"
    )
    enriched_count: int = Field(
        ..., ge=0, description="Number of entities successfully enriched"
    )
    failed_count: int = Field(
        ..., ge=0, description="Number of entities that failed enrichment"
    )
    batch_size_used: int | None = Field(
        default=None,
        ge=1,
        description="Effective batch size used for this run",
    )
    enrichment_version: str | None = Field(
        default=None,
        description="Version tag applied to enriched entities (from CODE_ENRICHMENT_VERSION env var)",
    )


__all__ = ["ModelCodeEnrichmentResult"]
