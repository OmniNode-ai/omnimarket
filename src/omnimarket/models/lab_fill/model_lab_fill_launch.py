# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The launch spec of one lab-fill lane, shared by the plan node and the effect node (OMN-20668).

The plan node decides which runner flags a lane carries; the effect node turns them
into the runner's arguments. Neither imports the other's models, so the spec lives here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")
LANE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"


class ModelLabFillApprovedRow(BaseModel):
    """The approved-work row a lane's brief names by id; its goal is read when the lane launches."""

    model_config = _FROZEN

    id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    kind: str
    ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    marker: str


class ModelLabFillLaunch(BaseModel):
    """The runner flags of one lane, as data: no shell text, no quoting."""

    model_config = _FROZEN

    lane: str = Field(pattern=LANE_PATTERN)
    ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    engine: Literal["sonnet", "codex"]
    effort: Literal["high"] = "high"
    host: str | None = None
    parent: str = Field(pattern=LANE_PATTERN)
    timeout_min: int = Field(ge=1, le=180)
    repo: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$")
    ref: str | None = None
    pr: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+#[0-9]+$")
    approved_row: ModelLabFillApprovedRow | None = None
