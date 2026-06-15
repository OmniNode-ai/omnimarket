# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for the context-artifact resolver (pure COMPUTE).

One request resolves the real text for every context factor that has at least
one source, materialising the ``artifact_content_map`` the ROI runner
(OMN-12798) consumes.  Budget and factor precedence are NOT reimplemented here:
the resolved artifacts are run through the existing
``HandlerContextPackBuilder`` so the 16k token-budget hard-reject and the
canonical precedence (golden_chain > exemplar > local_failures >
architecture_patterns > claude_md) are enforced by the one authority that owns
them.
"""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_context_artifact_resolver_compute.models.model_artifact_source import (
    ModelArtifactSource,
)

# Canonical factor precedence shared with the pack builder. Declared here as the
# resolver's default so a request without an explicit precedence still produces
# the same ordering the pack builder enforces.
CANONICAL_FACTOR_PRECEDENCE: tuple[EnumContextFactor, ...] = (
    EnumContextFactor.GOLDEN_CHAIN,
    EnumContextFactor.EXEMPLAR,
    EnumContextFactor.LOCAL_FAILURES,
    EnumContextFactor.ARCHITECTURE_PATTERNS,
    EnumContextFactor.CLAUDE_MD,
)


class ModelArtifactResolverRequest(BaseModel):
    """All inputs to materialise the per-factor content map.

    ``sources`` are already-read artifact bodies (the EFFECT boundary did the
    file I/O).  ``token_budget`` mirrors the pack-builder profile default of
    16000.  ``per_factor_token_budget`` bounds how much of a sectioned markdown
    source is selected for a single factor so that the union arm
    (structured_plus_guidance_chunks: all five factors) still fits the overall
    budget.
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
    token_budget: int = Field(
        default=16000,
        gt=0,
        description="Overall pack token budget; mirrors the pack-builder profile.",
    )
    per_factor_token_budget: int = Field(
        default=3000,
        gt=0,
        description=(
            "Maximum tokens selected for a single sectioned-markdown factor. "
            "Five factors x this budget must not exceed token_budget."
        ),
    )
    factor_precedence: tuple[EnumContextFactor, ...] = Field(
        default=CANONICAL_FACTOR_PRECEDENCE,
        description=(
            "Factor ordering passed to the pack builder. Defaults to the "
            "canonical precedence."
        ),
    )
    model_id: str = Field(
        default="resolver",
        min_length=1,
        description="Model id stamped on the pack-builder profile for provenance.",
    )


__all__ = ["CANONICAL_FACTOR_PRECEDENCE", "ModelArtifactResolverRequest"]
