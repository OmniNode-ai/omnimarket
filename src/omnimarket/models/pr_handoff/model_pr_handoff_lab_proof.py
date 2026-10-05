# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab proof a requesting lane cites for its PR (OMN-20636)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.pr_handoff.enum_pr_handoff_lab_proof_source import (
    EnumPrHandoffLabProofSource,
)


class ModelPrHandoffLabProof(BaseModel):
    """One Lab line, verbatim, and where the lane read it.

    The PR watcher's observation carries neither the PR body nor its comments,
    so the requesting lane quotes the line it read (the same read pr-handoff
    makes today). The decision compute checks the line against the live head:
    a comment's line must name ``head=<sha>`` of the live head, a body line
    must start with Lab, and the version-bump exemption needs a bot author.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: EnumPrHandoffLabProofSource
    line: str = Field(
        default="",
        max_length=2000,
        description="The Lab line verbatim; empty only for the version-bump exemption.",
    )


__all__: list[str] = ["ModelPrHandoffLabProof"]
