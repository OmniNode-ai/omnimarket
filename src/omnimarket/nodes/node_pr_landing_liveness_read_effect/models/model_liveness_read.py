# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the liveness read node is asked, what it returns and the two events it publishes (OMN-23529).

The completed event is the golden chain: a read that was made, carrying the liveness fact the landing decision reads. The failed
event is the error chain: a read that could not be made, with why; the fact it returns then says unreadable and names no file.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.landing_decision import (
    PR_KEY_PATTERN,
    SHA_PATTERN,
    ModelLandingLivenessFacts,
)


class ModelLivenessReadRequest(BaseModel):
    """One PR head to read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    head_sha: str = Field(..., pattern=SHA_PATTERN)


class ModelLivenessReadCompleted(BaseModel):
    """The golden chain event: the read was made."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    liveness: ModelLandingLivenessFacts


class ModelLivenessReadFailed(BaseModel):
    """The error chain event: the read could not be made, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    head_sha: str = Field(..., pattern=SHA_PATTERN)
    detail: str = Field(..., min_length=1)


class ModelLivenessReadResult(BaseModel):
    """The liveness fact for the PR head and the topic its event went to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    pr: str = Field(..., pattern=PR_KEY_PATTERN)
    liveness: ModelLandingLivenessFacts
    topic: str = Field(..., min_length=1)


__all__ = [
    "ModelLivenessReadCompleted",
    "ModelLivenessReadFailed",
    "ModelLivenessReadRequest",
    "ModelLivenessReadResult",
]
