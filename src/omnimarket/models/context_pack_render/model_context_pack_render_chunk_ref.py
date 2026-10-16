# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where a rendered chunk came from (OMN-21109, plan sections 4.2 and 4.6)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelContextPackRenderChunkRef(BaseModel):
    """Immutable source identity of one chunk, keyed by the pack chunk's id.

    source_identifier is path@sha, an invariant id@sha with its line span, a blob sha or
    an exemplar's gate receipt: it changes only when the pinned source changes, so a stable
    block keeps it. row_timestamp and row_lane are the reference of a ledger row, required
    for an operator ruling.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str = Field(min_length=1)
    source_identifier: str | None = Field(default=None, min_length=1)
    row_timestamp: str | None = Field(default=None, min_length=1)
    row_lane: str | None = Field(default=None, min_length=1)
