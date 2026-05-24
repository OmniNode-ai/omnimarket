# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for the knowledge health probe effect node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelKnowledgeHealthProbeRequest(BaseModel):
    """Parameters controlling which backends to probe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backends: tuple[str, ...] = Field(
        default=("repowise", "qdrant", "memgraph", "kb_repo", "agent_learning"),
        description="Backend IDs to probe. Defaults to all known backends.",
    )
    repowise_url: str | None = Field(
        default=None,
        description="Repowise API base URL. Falls back to REPOWISE_URL env var.",
    )
    qdrant_url: str | None = Field(
        default=None,
        description="Qdrant API base URL. Falls back to QDRANT_URL env var.",
    )
    memgraph_bolt_url: str | None = Field(
        default=None,
        description="Memgraph Bolt URL. Falls back to MEMGRAPH_BOLT_URL env var.",
    )
    kb_repo_path: str | None = Field(
        default=None,
        description="Path to the KB repository root for last-commit checks.",
    )
    timeout_seconds: float = Field(default=10.0, ge=0.1, le=120.0)


__all__ = ["ModelKnowledgeHealthProbeRequest"]
