# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for the context-artifact resolver (pure COMPUTE).

One request resolves every pre-read source into ``ModelContextPackArtifact``
entries.  The resolver carries no budget, precedence or selection input:
node_context_pack_builder_compute owns selection, the token budget, factor
precedence and the pack hash, and the resolver's artifacts go to it whole
(OMN-21137).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_context_artifact_resolver_compute.models.model_artifact_source import (
    ModelArtifactSource,
)


class ModelArtifactResolverRequest(BaseModel):
    """All inputs to resolve pre-read sources into pack artifacts.

    ``sources`` are already-read artifact bodies (the EFFECT boundary did the
    file I/O).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_hash: str = Field(
        min_length=1,
        description="SHA-256 of the resolver contract for provenance.",
    )
    generated_at: str = Field(
        min_length=1,
        description="Caller-supplied timezone-aware ISO8601 UTC timestamp.",
    )
    sources: tuple[ModelArtifactSource, ...] = Field(
        description="Pre-read artifact sources, one or more per factor."
    )


__all__ = ["ModelArtifactResolverRequest"]
