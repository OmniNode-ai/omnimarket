# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Input model for node_antipattern_index_effect. [OMN-11913]"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAntipatternIndexRequest(BaseModel):
    """Command payload for antipattern registry indexing."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str = Field(..., min_length=1, description="Propagated to result")
    repo_root: str | None = Field(
        default=None,
        description="Repo root for per-repo override resolution. Uses cwd if None.",
    )
    force_reindex: bool = Field(
        default=False,
        description="Force re-indexing even if registry version is already indexed.",
    )
    embedding_endpoint_override: str | None = Field(
        default=None,
        description="Override EMBEDDING_MODEL_URL for this invocation.",
    )
    qdrant_collection_override: str | None = Field(
        default=None,
        description="Override ANTIPATTERN_QDRANT_COLLECTION for this invocation.",
    )


__all__ = ["ModelAntipatternIndexRequest"]
