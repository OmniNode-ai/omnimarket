# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab proof a requesting lane cites for its PR (OMN-20636)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.pr_handoff.enum_pr_handoff_lab_proof_source import (
    EnumPrHandoffLabProofSource,
)


class ModelPrHandoffLabProof(BaseModel):
    """The lab proof, verbatim, and where the lane read it.

    The PR watcher's observation carries neither the PR body nor its comments,
    so the requesting lane quotes what it read (the same read pr-handoff makes
    today). The decision compute checks it against the live head: a comment
    must have a line starting Lab and name ``head=<sha>`` of the live head, and
    a body line must contain Lab. Local mode's bot version-bump exemption is
    not offered here: it needs the PR's changed files, which no observation
    carries (pr-handoff --via local keeps it).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: EnumPrHandoffLabProofSource
    line: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="The body's Lab line, or the whole author comment that carries one, verbatim.",
    )


__all__: list[str] = ["ModelPrHandoffLabProof"]
