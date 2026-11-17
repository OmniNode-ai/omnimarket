# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and results for the stall episode judgment of work progress compute.

A watched thing (a lane, a pull request, a scheduled round or a host tick) is in one phase at a time.
Its phase has a time budget. The instant the budget clock started is the thing's anchor; the anchor
identifies the entry or the last movement (a ledger row id, a head sha, a tick id). A thing that stays
over budget on one anchor is one crossing, so the identity of a crossing is
``(kind, key, phase, anchor_id)`` and never the wall clock. A thing that moves, changes phase or leaves
the reading closes its episode; a later stall is a new crossing under a new anchor.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    field_validator,
    model_validator,
)

from omnimarket.models.lab_alarm_delivery import ModelLabAlarmDeliveryRequest
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)

#: The one phase of a scheduled round: the time since its last heartbeat.
ROUND_PHASE = "awaiting_next_run"


class EnumWatchedKind(StrEnum):
    LANE = "lane"
    PR = "pr"
    ROUND = "round"
    TICK = "tick"


class EnumEpisodeClosed(StrEnum):
    """Why a crossed thing's episode closed."""

    #: The thing is in another phase.
    PHASE_CHANGED = "phase-changed"
    #: The thing is in the same phase on a newer anchor: it moved (a later stall is a new crossing).
    MOVED = "moved"
    #: The thing is absent from a reading of its kind that is complete (it ended).
    GONE = "gone"


def _no_separator(value: str) -> str:
    if "|" in value:
        raise ValueError("must not contain the key separator '|'")
    return value


def crossing_id(kind: EnumWatchedKind, key: str, phase: str, anchor_id: str) -> str:
    """The identity of one crossing: the thing, its phase and the anchor of the phase's clock."""
    return f"{kind.value}|{key}|{phase}|{anchor_id}"


class ModelWatchedThing(BaseModel):
    """One watched thing and the phase it is in, supplied by the caller's reader."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumWatchedKind
    key: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    #: The instant the phase's budget clock started (its entry, or its last movement).
    anchor_at: AwareDatetime
    #: What that instant is (a ledger row id, a head sha, a tick id); a new anchor is a new episode.
    anchor_id: str = Field(min_length=1)
    owner: str = Field(default="unassigned", min_length=1)

    @field_validator("key", "phase", "anchor_id")
    @classmethod
    def _separator(cls, value: str) -> str:
        return _no_separator(value)


class ModelOpenEpisode(BaseModel):
    """A crossing already alerted and not yet closed; the caller carries it to the next evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    crossing_id: str = Field(min_length=1)
    kind: EnumWatchedKind
    key: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    anchor_id: str = Field(min_length=1)
    crossed_at: AwareDatetime
    owner: str = Field(default="unassigned", min_length=1)


class ModelWorkEpisodeRequest(BaseModel):
    """Everything one judgment needs; the handler reads no clock, file or store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    now: AwareDatetime
    #: Seconds per phase per kind, from the overlay.
    budgets: Mapping[str, Mapping[str, PositiveInt]]
    things: tuple[ModelWatchedThing, ...] = ()
    #: Scheduled round heartbeats (``ModelScheduleRunEvent``); the newest per job is that round's anchor.
    round_heartbeats: tuple[ModelScheduleRunEvent, ...] = ()
    #: Per-job override of the round budget (seconds), from the overlay.
    round_job_budgets: Mapping[str, PositiveInt] = Field(default_factory=dict)
    open_episodes: tuple[ModelOpenEpisode, ...] = ()
    #: Kinds whose reading enumerated every open thing; only these let an absent thing close an episode.
    complete_kinds: tuple[EnumWatchedKind, ...] = ()
    #: The condition name the alarm node keys its episodes on.
    alarm_condition: str = Field(default="work_stall", min_length=1)

    @model_validator(mode="after")
    def _budgets_present(self) -> Self:
        seen: set[tuple[EnumWatchedKind, str]] = set()
        for thing in self.things:
            ref = (thing.kind, thing.key)
            if ref in seen:
                raise ValueError(f"two readings of {thing.kind.value} {thing.key!r}")
            seen.add(ref)
            if thing.phase not in self.budgets.get(thing.kind.value, {}):
                raise ValueError(
                    f"no budget for {thing.kind.value} phase {thing.phase!r}"
                )
        if self.round_heartbeats and ROUND_PHASE not in self.budgets.get(
            EnumWatchedKind.ROUND.value, {}
        ):
            raise ValueError(f"no budget for round phase {ROUND_PHASE!r}")
        return self


class ModelWorkCrossing(BaseModel):
    """A thing newly over the budget of its phase: one alert."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    crossing_id: str
    kind: EnumWatchedKind
    key: str
    phase: str
    anchor_id: str
    budget_seconds: int
    spent_seconds: int
    owner: str
    text: str


class ModelWorkRecovery(BaseModel):
    """An open episode that closed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    crossing_id: str
    kind: EnumWatchedKind
    key: str
    phase: str
    reason: EnumEpisodeClosed
    text: str


class ModelWorkEpisodeResult(BaseModel):
    """New crossings, closed episodes, the episodes still open, and the alarm node's input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    crossings: tuple[ModelWorkCrossing, ...] = ()
    recoveries: tuple[ModelWorkRecovery, ...] = ()
    still_open: tuple[ModelOpenEpisode, ...] = ()
    #: still_open plus the new crossings: the state to hand back as ``open_episodes`` next time.
    open_episodes: tuple[ModelOpenEpisode, ...] = ()
    #: ALARM for new crossings; OK when the last open episode closed; None when there is nothing to deliver.
    alarm_request: ModelLabAlarmDeliveryRequest | None = None


__all__ = [
    "ROUND_PHASE",
    "EnumEpisodeClosed",
    "EnumWatchedKind",
    "ModelOpenEpisode",
    "ModelWatchedThing",
    "ModelWorkCrossing",
    "ModelWorkEpisodeRequest",
    "ModelWorkEpisodeResult",
    "ModelWorkRecovery",
    "crossing_id",
]
