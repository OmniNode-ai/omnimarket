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
# OMN-22866: lane-brief's ``check --lab`` (lab_child_id_problems, OMN-22883) refuses a lab-bound brief whose own prose opens a
# pull request and carries neither a ``child-ticket-ids:`` line nor a push-the-branch-and-report sentence. Every fill brief that
# opens a pull request was refused from the first fire after that check was installed (2026-11-08 21:57Z: 1 to 7 eligible
# tickets, dispatched=0, 41 free slots). A fill lane has no ticket id to wait for, so it pushes and names its branch and head.
# The sentence holds none of the negation words the check skips a sentence for (never, not, nothing, without, refus*, avoid*,
# n't), or the check would not count it as an instruction.
PUSH_AND_REPORT_LINE = "Push the branch when the work is ready, and name its branch and head in your TERMINAL."


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
    # OMN-21881: ``auto`` lets the runner place a repo-only lane on a lab host first and a
    # cloud session as the overflow; None passes no --placement (the runner's own default).
    placement: Literal["auto"] | None = None
    # OMN-22413: the runner's queue class for work that fixes the factory itself.
    priority_class: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]*$")
