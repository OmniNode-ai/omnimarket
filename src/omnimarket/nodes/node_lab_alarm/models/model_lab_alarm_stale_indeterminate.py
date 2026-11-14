# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the lab alarm's stale-indeterminate condition."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumStaleIndeterminateOutcome(StrEnum):
    """OK: no watched condition is stale. ALARM: a watched condition has read INDETERMINATE on its last runs. INDETERMINATE: the record store is unreadable."""

    OK = "OK"
    ALARM = "ALARM"
    INDETERMINATE = "INDETERMINATE"


class EnumStaleIndeterminateFault(StrEnum):
    """The named breaks a drill injects; a drill keeps no state and delivers nothing."""

    NONE = "none"
    #: The first watched condition reads as an injected stale verdict that bypasses persistence; nothing is read, kept or delivered.
    PLANTED_STALE = "planted-stale"


class ModelStaleIndeterminateCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: False reads without delivering the verdict, so a probe changes nothing.
    write: bool = True
    fault: EnumStaleIndeterminateFault = EnumStaleIndeterminateFault.NONE


class ModelStaleIndeterminateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumStaleIndeterminateOutcome
    fault: EnumStaleIndeterminateFault
    conditions: tuple[str, ...]
    readings: tuple[str, ...]
    alarms: tuple[str, ...]
    #: Why the run is INDETERMINATE, or which conditions are still waiting on their streak.
    reasons: tuple[str, ...]
    evidence: str
    failure: str = ""
    delivery: tuple[str, ...] = ()


__all__ = [
    "EnumStaleIndeterminateFault",
    "EnumStaleIndeterminateOutcome",
    "ModelStaleIndeterminateCommand",
    "ModelStaleIndeterminateResult",
]
