# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One Claude review call's response usage on one key (OMN-20967).

The budget compute reads a key's recorded calls in this shape. It is shared
here, not in the node, so the spend record that writes the calls can use it
without importing the compute's models. A key is named by its reference, never
its value.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelClaudeReviewUsage(BaseModel):
    """The four token counts of one call's response ``usage``, on one key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: UUID
    api_key_ref: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    observed_at: AwareDatetime
    input_tokens: int = Field(..., ge=0, description="usage.input_tokens (uncached).")
    cache_creation_input_tokens: int = Field(
        ..., ge=0, description="usage.cache_creation_input_tokens (cache write)."
    )
    cache_read_input_tokens: int = Field(
        ..., ge=0, description="usage.cache_read_input_tokens (cache read)."
    )
    output_tokens: int = Field(..., ge=0, description="usage.output_tokens.")


__all__ = ["ModelClaudeReviewUsage"]
