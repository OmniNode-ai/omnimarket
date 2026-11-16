# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The automation-liveness read for one PR head, as the landing decision reads it (OMN-23529).

The landing snapshot step runs the liveness register's completeness reader against the PR's head and hands the
decision one fact per PR: whether the read was made, and the installer sources the head adds that the register does
not declare. A read that was made and names a file holds the head with the file named; a read that was not made
(unreadable, or absent where the policy expects one) holds nothing and the decision says the read was not made, as the
old landing controller does. This node only compares: the reader, the checkout and the timeouts belong to the step
that runs the read and come from its overlay.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    SHA_PATTERN,
)


class EnumLivenessRead(StrEnum):
    READ = "read"
    UNREADABLE = "unreadable"


class ModelLandingLivenessFacts(BaseModel):
    """One PR head's liveness read: the read's outcome and every undeclared installer source it named."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    head_sha: str = Field(..., pattern=SHA_PATTERN)
    read: EnumLivenessRead
    undeclared_sources: tuple[str, ...] = Field(
        default=(),
        description=(
            "Repository paths of installer sources this head adds that the liveness register does not declare; "
            "empty on a read that found none."
        ),
    )
    detail: str = Field(
        default="", description="Why the read was not made, when it was not."
    )

    @model_validator(mode="after")
    def _unreadable_names_no_file(self) -> ModelLandingLivenessFacts:
        if self.read is EnumLivenessRead.UNREADABLE and self.undeclared_sources:
            raise ValueError("an unreadable liveness read names no undeclared source")
        if self.read is EnumLivenessRead.UNREADABLE and not self.detail:
            raise ValueError("an unreadable liveness read says why it was not made")
        return self


class ModelLandingLivenessUnread(BaseModel):
    """A PR whose liveness read was not made this tick: it is not held, and the decision says so."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str = Field(..., min_length=1)
    head_sha: str = Field(..., pattern=SHA_PATTERN)
    detail: str = Field(..., min_length=1)


__all__ = [
    "EnumLivenessRead",
    "ModelLandingLivenessFacts",
    "ModelLandingLivenessUnread",
]
