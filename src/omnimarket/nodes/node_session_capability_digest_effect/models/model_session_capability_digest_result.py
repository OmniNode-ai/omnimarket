# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the session-start hook prints, and why."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumSessionCapabilityDigestOutcome(StrEnum):
    """The one reason a session start prints what it prints."""

    SILENT_OUTSIDE_ROOTS = "silent_outside_roots"
    DIGEST = "digest"
    STALE = "stale"
    COMPACT_POINTER = "compact_pointer"


class ModelSessionCapabilityDigestResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumSessionCapabilityDigestOutcome = Field(
        description="Why this text was chosen."
    )
    output: str = Field(
        description="Text for stdout; empty when the hook stays silent."
    )
    age_minutes: int | None = Field(
        default=None,
        description="Whole minutes since the digest was written; None when it is missing or unread.",
    )
