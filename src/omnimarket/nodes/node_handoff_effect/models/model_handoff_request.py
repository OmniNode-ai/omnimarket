# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed command payload for the handoff effect."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelHandoffRequest(BaseModel):
    """Request to capture deterministic session handoff state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(..., min_length=1)
    correlation_id: UUID
    summary: str | None = Field(default=None)
    cwd: str | None = Field(default=None)


__all__ = ["ModelHandoffRequest"]
