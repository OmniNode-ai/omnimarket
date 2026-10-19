# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for the context-artifact resolver (pure COMPUTE).

The headline output is ``artifacts``: one ``ModelContextPackArtifact`` per
whole-body source and one per markdown section of a sectioned source, each
with its provenance (``source_file``, ``heading_path``, ``char_count``,
``reason_selected``).  Nothing is selected, deduplicated, budgeted or hashed
here; the ROI runner passes these artifacts to
node_context_pack_builder_compute, which does all four (OMN-21137).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.pack.model_context_pack_artifact import ModelContextPackArtifact


class EnumArtifactResolverStatus(StrEnum):
    """Lifecycle state for artifact resolution."""

    OK = "ok"
    FAILED = "failed"


class ModelArtifactResolverResult(BaseModel):
    """Resolved pack artifacts plus the factors they cover."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumArtifactResolverStatus
    artifacts: tuple[ModelContextPackArtifact, ...] = Field(
        default_factory=tuple,
        description=(
            "Resolved artifacts in source order, then section order. "
            "Empty when status is FAILED."
        ),
    )
    resolved_factors: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Factor value strings with at least one artifact, first-seen order.",
    )
    errors: tuple[str, ...] = Field(default_factory=tuple)
    warnings: tuple[str, ...] = Field(default_factory=tuple)


__all__ = ["EnumArtifactResolverStatus", "ModelArtifactResolverResult"]
