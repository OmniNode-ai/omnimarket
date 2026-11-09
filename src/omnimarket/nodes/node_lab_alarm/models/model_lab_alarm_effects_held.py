# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the lab alarm's effects-held condition (OMN-23106)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumEffectsHeldOutcome(StrEnum):
    """OK: a group is live, or the hold is under its bound. ALARM: held at or past the bound. INDETERMINATE: not read."""

    OK = "OK"
    ALARM = "ALARM"
    INDETERMINATE = "INDETERMINATE"


class EnumEffectsHeldFault(StrEnum):
    """The named breaks a drill injects; a drill keeps no state and opens no lane."""

    NONE = "none"
    #: Every matching group reads as having no live member, and the hold reads as already past its bound.
    CONSUMER_ABSENT = "consumer-absent"


class EnumEscalation(StrEnum):
    """What the run did about an alarm: NOT_NEEDED outside one, DISPATCHED opened a lane, ALREADY kept the episode's, FAILED did not start one."""

    NOT_NEEDED = "not-needed"
    DISPATCHED = "dispatched"
    ALREADY = "already-dispatched"
    FAILED = "failed"
    DRILL = "drill"


class ModelEffectsHeldCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: False reads without keeping the hold clock or opening a lane, so a probe changes nothing.
    write: bool = True
    fault: EnumEffectsHeldFault = EnumEffectsHeldFault.NONE


class ModelEffectsGroupReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    group: str
    state: str
    members: int = Field(ge=0)


class ModelEscalationBrief(BaseModel):
    """What the lane that owns the condition is told: the condition, since when, and what was read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    condition: str
    subject: str
    raised_at: str
    detail: str
    evidence: str


class ModelEffectsHeldResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumEffectsHeldOutcome
    fault: EnumEffectsHeldFault
    readings: tuple[ModelEffectsGroupReading, ...]
    #: When the hold began, when the consumer is absent; empty when a group is live.
    held_since: str
    held_for_seconds: float
    bound_seconds: float
    escalation: EnumEscalation
    #: Why the lane did not start, or why the run is INDETERMINATE.
    reasons: tuple[str, ...]
    state_kept: bool
    evidence: str
    failure: str = ""


__all__ = [
    "EnumEffectsHeldFault",
    "EnumEffectsHeldOutcome",
    "EnumEscalation",
    "ModelEffectsGroupReading",
    "ModelEffectsHeldCommand",
    "ModelEffectsHeldResult",
    "ModelEscalationBrief",
]
