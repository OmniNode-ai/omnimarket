# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelReplayExpectation — expected replay result for divergence comparison."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelReplayExpectation(BaseModel):
    """Expected replay result used for divergence comparison.

    Attributes:
        projection_checksum: The expected projection checksum. Divergence in
            this value proves a content/ordering divergence.
        cursor_token: The expected cursor token. Divergence in this value proves
            a delivery-position divergence (dropped/added/re-offset events).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    projection_checksum: str = Field(
        ..., min_length=1, description="Expected projection checksum."
    )
    cursor_token: str = Field(..., min_length=1, description="Expected cursor token.")


__all__ = ["ModelReplayExpectation"]
