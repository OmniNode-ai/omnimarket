# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The settled state of every lane of one lab-fill run (OMN-23025)."""

from pydantic import BaseModel, ConfigDict


class ModelLabFillLaneStatesRequest(BaseModel):
    """The run's placements and fallbacks as the placement step returned them.

    claimed_lanes are the lanes that have a CLAIM row on a host by the time the caller reads
    the ledger: a lane whose send job was still building when the STATUS row was composed
    reads pending in its placement, and a CLAIM is what makes it placed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    placements: tuple[dict[str, object], ...] = ()
    fallbacks: tuple[dict[str, object], ...] = ()
    claimed_lanes: tuple[str, ...] = ()


class ModelLabFillLaneState(BaseModel):
    """One lane, its settled state and the reason its dispatch gave when it never started."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    lane: str
    host: str = ""
    state: str
    reason: str = ""


class ModelLabFillLaneStatesResult(BaseModel):
    """The cells the STATUS row appends after lanes=, the final placed count and the named lanes."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    states_cell: str
    refusals_cell: str
    placed: int
    never_started: tuple[str, ...] = ()
    lanes: tuple[ModelLabFillLaneState, ...] = ()
