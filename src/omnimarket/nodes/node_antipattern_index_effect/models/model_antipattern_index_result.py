# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Output model for node_antipattern_index_effect. [OMN-11913]"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAntipatternIndexResult(BaseModel):
    """Result of an antipattern registry indexing run."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str = Field(..., min_length=1, description="Propagated from input")
    indexed_count: int = Field(
        ..., ge=0, description="Entries successfully embedded and upserted"
    )
    skipped_count: int = Field(
        ..., ge=0, description="Entries skipped (not vector_enabled or failed)"
    )
    registry_version: str = Field(..., description="Registry version that was indexed")
    vector_ids: list[str] = Field(
        default_factory=list,
        description="Qdrant point IDs of successfully upserted vectors",
    )
    qdrant_collection: str | None = Field(
        default=None,
        description="Qdrant collection name used for this run",
    )
    was_no_op: bool = Field(
        default=False,
        description="True if the registry version was already indexed and force_reindex was False",
    )


__all__ = ["ModelAntipatternIndexResult"]
