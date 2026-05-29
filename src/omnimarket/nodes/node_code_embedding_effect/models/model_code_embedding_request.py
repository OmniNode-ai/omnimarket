# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed command payload for code embedding batches."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCodeEmbeddingRequest(BaseModel):
    """Request to embed pending code entities into the vector index."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., min_length=1)
    batch_size: int | None = Field(default=None, gt=0)
    embedding_endpoint_override: str | None = Field(default=None)
    qdrant_collection_override: str | None = Field(default=None)


__all__ = ["ModelCodeEmbeddingRequest"]
