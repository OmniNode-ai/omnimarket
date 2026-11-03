# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ledger guard's refusal of a replayed append, and what the actor did with it (OMN-21959)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumGuardRefusal(StrEnum):
    """The ledger guards whose refusal of a queued row is told apart by what corrects it."""

    # OMN-18274: a TERMINAL cites a friction and no FRICTION row exists for its lane
    FRICTION_GUARD = "friction-guard"
    # OMN-20148: the TERMINAL says removed:<path> while the path still exists
    WORKTREE_REMOVED_EXISTS = "worktree-removed-exists"
    # OMN-20148: the TERMINAL says kept:<path>, which is not the worktree its CLAIM named
    WORKTREE_KEPT_MISMATCH = "worktree-kept-mismatch"


class ModelGuardRefusal(BaseModel):
    """What one recorded refusal says, read from the case's own words.

    ``lost_stamp`` is the stamp of the TERMINAL that was lost ("" when the evidence names none),
    ``refused_path`` is where the writer's machine kept the refused request ("" when not named) and
    ``cell`` is the worktree path the guard named ("" for the friction guard). ``lost_rows`` names each
    row the refusal lost as ``<KIND>@<stamp>`` (empty when the evidence names none).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    guard: EnumGuardRefusal
    lane: str
    lost_stamp: str
    refused_path: str
    cell: str
    lost_rows: tuple[str, ...] = ()


class ModelGuardResolution(BaseModel):
    """One case after the actor: resolved in code, or left open with what is missing named."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    key: str
    resolved: bool
    how: str
    emitted: str  # "<status>:<id>" of the queue row left open; "" when resolved or left to the operator
