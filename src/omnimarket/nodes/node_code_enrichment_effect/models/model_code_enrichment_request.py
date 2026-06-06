# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed command payload for code enrichment batches."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCodeEnrichmentRequest(BaseModel):
    """Request to enrich pending code entities with LLM-derived metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., min_length=1)
    batch_size: int | None = Field(default=None, gt=0)
    llm_endpoint_override: str | None = Field(default=None)


__all__ = ["ModelCodeEnrichmentRequest"]
