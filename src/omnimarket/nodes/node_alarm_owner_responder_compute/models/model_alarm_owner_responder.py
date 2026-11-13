# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The models of node_alarm_owner_responder_compute (OMN-24200).

The request carries the FRICTION rows the lab alarm writes, the policy that decides which
conditions require an owner and the bounds of the dispatch window, and the state of the lanes
already live. The result names the cause keys that start a lane and the ones that are skipped.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.model_stall_cause import (
    ModelCauseDispatch,
    ModelCauseSkip,
    ModelRowRefusal,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"


class ModelAlarmOwnerPolicy(BaseModel):
    """The policy that decides which alarm conditions require an owner and when."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The actor cell of the alarm writer; only its rows are read.
    alarm_actor: str = Field(min_length=1)
    #: The alarm conditions that require an owner.
    conditions: tuple[str, ...] = Field(min_length=1)
    #: A cause key dispatches only when its first raise is this many hours old.
    owner_required_after_hours: float = Field(gt=0)
    #: A dispatched cause key dispatches again only this many hours after its last dispatch.
    redispatch_after_hours: float = Field(gt=0)


class ModelAlarmOwnerRequest(BaseModel):
    """The FRICTION rows, the policy, the clock and the lanes already live."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The FRICTION rows the lab alarm wrote, one per line.
    rows: tuple[str, ...]
    #: The clock: a UTC stamp the node has no clock of its own.
    now: str
    #: The policy that decides which conditions require an owner and when.
    policy: ModelAlarmOwnerPolicy
    #: Cause key -> stamp of the last dispatch.
    dispatched: dict[str, str] = {}
    #: Cause keys that a live lane's CLAIM names.
    covered_causes: tuple[str, ...] = ()
    #: Names of live lanes.
    covered_lanes: tuple[str, ...] = ()

    @field_validator("now")
    @classmethod
    def _now_is_a_real_calendar_date(cls, value: str) -> str:
        try:
            datetime.strptime(value, _STAMP)
        except ValueError as exc:
            raise ValueError(
                "now must be a UTC stamp of the form YYYY-MM-DDTHH:MM:SSZ"
            ) from exc
        return value


class ModelAlarmOwnerResult(BaseModel):
    """The cause keys that start a lane and the ones that are skipped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The cause keys that start a lane.
    dispatches: tuple[ModelCauseDispatch, ...]
    #: The cause keys that are skipped, with the reason.
    skipped: tuple[ModelCauseSkip, ...]
    #: Cause key -> stamp of the dispatch this run made.
    dispatched: dict[str, str]
    #: The FRICTION rows that passed the actor and the calendar checks.
    alarm_rows: int
    #: The rows whose condition is not in the policy's conditions.
    outside_conditions: int
    #: The rows refused alone, with the reason.
    refusals: tuple[ModelRowRefusal, ...] = ()


__all__ = [
    "ModelAlarmOwnerPolicy",
    "ModelAlarmOwnerRequest",
    "ModelAlarmOwnerResult",
]
