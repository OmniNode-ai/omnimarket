# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One hook refusal, as the guard states it, before redaction or dedupe."""

from pydantic import BaseModel, ConfigDict, Field


class ModelHookRefusalRowRequest(BaseModel):
    """Raw refusal facts; the handler redacts, normalises and keys them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    guard: str = Field(description="The refusing guard's id, unredacted.")
    reason: str = Field(description="The guard's reason text, unredacted.")
    detail: str = Field(default="", description="The refusal's first line.")
    lane: str = Field(
        default="", description="The resolved lane; empty when unresolved."
    )
    lane_source: str = Field(
        default="unresolved", description="How the lane was resolved."
    )
    session: str = Field(
        default="",
        description="Session scope for the secret guard's retry budget; empty when none.",
    )
    suppressed: int = Field(
        default=0,
        ge=0,
        description="Refusals of this key swallowed since the last emitted row.",
    )
    timestamp: str = Field(
        description="UTC timestamp of the row, YYYY-MM-DDTHH:MM:SSZ."
    )
