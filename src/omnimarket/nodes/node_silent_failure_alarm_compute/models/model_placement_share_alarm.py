# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab-fill runs and lane refusals in, the placement-share alarm verdict out (OMN-17427)."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelPlacementRun(BaseModel):
    """One lab-fill run as its STATUS row states it: lanes dispatched and lanes placed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_key: str = Field(
        min_length=1, description="The run's MMDDHHMM key, as its lanes carry it."
    )
    stamp: AwareDatetime
    dispatched: int = Field(ge=0)
    placed: int = Field(ge=0)


class ModelLaneRefusal(BaseModel):
    """One lane's terminal receipt that ended in a refusal, with the run it belongs to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_key: str = Field(min_length=1)
    lane: str
    status: str = Field(min_length=1)


class ModelPlacementShareRequest(BaseModel):
    """The facts a caller read; the node holds no clock, no file and no ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    runs: tuple[ModelPlacementRun, ...] = ()
    refusals: tuple[ModelLaneRefusal, ...] = ()
    # Placed under this share of dispatched is a bad run; two bad acting runs in a row alarm.
    share_floor: float = Field(default=0.5, gt=0, le=1)
    consecutive_runs: int = Field(default=2, ge=1)
    # One refusal reason on more than this share of a run's lanes alarms on its own.
    refusal_share: float = Field(default=0.5, gt=0, lt=1)
    refusal_min_lanes: int = Field(default=2, ge=1)


class ModelPlacementShareResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    alarm: bool
    kinds: tuple[Literal["placement-share", "refusal-reason"], ...] = ()
    # The refusal reason the alarm names; "no-refusal-reported" when no receipt names one.
    reason: str = ""
    runs: tuple[str, ...] = ()
    dispatched: int = 0
    placed: int = 0
    friction_cells: str = ""
    alert_text: str = ""
