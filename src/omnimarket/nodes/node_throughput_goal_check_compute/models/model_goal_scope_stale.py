# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the goal check's stale-scope check (OMN-26522).

The request carries the two texts the caller read: the goal_scope overlay and the rolling ledger. The
result carries the lines the check prints, none when the overlay is current.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelGoalScopeStaleRequest(BaseModel):
    """The overlay text and the ledger text the stale-scope check compares."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_text: str = Field(
        description="The goal_scope overlay: `key: scalar` lines, `#` comments; its ruling_date is read."
    )
    ledger_text: str = Field(
        description="The rolling ledger: rows of ` | `-separated cells, `<ts> | <TYPE> | key=value | ... | text`."
    )


class ModelGoalScopeStaleResult(BaseModel):
    """The lines the check prints: MISSING, UNKNOWN or NOTE lines, none when the overlay is current."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: list[str] = Field(default_factory=list)


__all__ = ["ModelGoalScopeStaleRequest", "ModelGoalScopeStaleResult"]
