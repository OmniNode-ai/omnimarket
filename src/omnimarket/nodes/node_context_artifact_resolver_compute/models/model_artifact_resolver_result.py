# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for the context-artifact resolver (pure COMPUTE).

The headline output is ``artifact_content_map``: a mapping from
EnumContextFactor *value* string (e.g. "golden_chain") to the resolved,
budget-bounded, precedence-ordered text the ROI runner injects.  The runner's
``_assemble_context_text`` consumes this map verbatim, so an ON arm that lists
a factor receives real content instead of the ``[stub content for ...]``
placeholder.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumArtifactResolverStatus(StrEnum):
    """Lifecycle state for artifact resolution."""

    OK = "ok"
    FAILED = "failed"


class ModelArtifactResolverResult(BaseModel):
    """Resolved per-factor content map plus provenance.

    ``artifact_content_map`` is keyed by EnumContextFactor value string and is
    the exact shape ``ModelContextRoiRunRequest.artifact_content_map`` expects.
    ``pack_hash`` is the hash of the pack the budget/precedence authority
    produced, for replay provenance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumArtifactResolverStatus
    artifact_content_map: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "EnumContextFactor value -> resolved factor text. "
            "Empty when status is FAILED."
        ),
    )
    resolved_factors: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Factor value strings that produced non-empty content.",
    )
    pack_hash: str | None = Field(
        default=None,
        description="Hash of the budget-enforced pack, for replay provenance.",
    )
    total_token_estimate: int = Field(
        default=0,
        ge=0,
        description="Total estimated tokens across the resolved content map.",
    )
    failure_class: str | None = Field(
        default=None,
        description="Pack-builder failure class when status is FAILED.",
    )
    errors: tuple[str, ...] = Field(default_factory=tuple)
    warnings: tuple[str, ...] = Field(default_factory=tuple)


__all__ = ["EnumArtifactResolverStatus", "ModelArtifactResolverResult"]
