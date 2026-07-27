# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input claim models for report content-anchor probes (OMN-15164).

A "claim" is one field's worth of content-anchor value lifted off a dispatch
report (``omnibase_core.models.dispatch.report``, OMN-15161) by the caller --
this node never imports or introspects that report model itself (it stays a
plain typed-claim I/O surface per the OMN-15164 brief), so it works whether or
not the caller's omnibase_core pin exposes the ported report models yet.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelShaAnchorClaim(BaseModel):
    """One ``*_sha``-suffixed report field to resolve against a git_dir."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_name: str = Field(..., description="Report field name (e.g. 'head_sha').")
    sha: str = Field(..., description="Claimed commit SHA to resolve.")


class ModelPathAnchorClaim(BaseModel):
    """One ``*_paths``-suffixed report field entry to check under repo_root."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_name: str = Field(
        ..., description="Report field name (e.g. 'evidence_paths')."
    )
    path: str = Field(..., description="Claimed artifact path, relative to repo_root.")


class ModelPrAnchorClaim(BaseModel):
    """The optional PR-number claim to confirm via ``gh pr view``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_name: str = Field(..., description="Report field name (e.g. 'pr_number').")
    pr_number: int = Field(..., gt=0, description="Claimed PR number.")
    repo: str = Field(
        ..., description="'<owner>/<name>' GitHub repo slug to confirm the PR against."
    )


__all__ = [
    "ModelPathAnchorClaim",
    "ModelPrAnchorClaim",
    "ModelShaAnchorClaim",
]
