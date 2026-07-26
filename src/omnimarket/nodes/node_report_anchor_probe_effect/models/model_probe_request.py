# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for the report anchor-probe EFFECT (OMN-15164)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_report_anchor_probe_effect.models.model_anchor_claim import (
    ModelPathAnchorClaim,
    ModelPrAnchorClaim,
    ModelShaAnchorClaim,
)


class ModelReportAnchorProbeRequest(BaseModel):
    """Anchor claims to probe against live repo/PR state.

    ``git_dir``/``repo_root`` are the checking CONTEXT, not claims -- a claim
    present with its context withheld is a fail-closed
    :class:`~omnimarket.nodes.node_report_anchor_probe_effect.models.model_probe_status.EnumAnchorProbeStatus.MISSING_CONTEXT`,
    never a silent skip (mirrors the ported core anchor library's fail-closed
    semantics, OMN-15161).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Runtime correlation ID.")
    sha_claims: tuple[ModelShaAnchorClaim, ...] = Field(
        default=(), description="*_sha content-anchor claims to resolve."
    )
    path_claims: tuple[ModelPathAnchorClaim, ...] = Field(
        default=(), description="*_paths content-anchor claims to check."
    )
    pr_claim: ModelPrAnchorClaim | None = Field(
        default=None, description="Optional PR-number claim to confirm via gh."
    )
    git_dir: str | None = Field(
        default=None,
        description="git dir (e.g. <worktree>/.git) used to resolve sha_claims.",
    )
    repo_root: str | None = Field(
        default=None,
        description="Repo root used to resolve+contain path_claims.",
    )


__all__ = ["ModelReportAnchorProbeRequest"]
