# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the lab alarm's UPS condition."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumUpsOutcome(StrEnum):
    """OK: no rule tripped. ALARM: at least one rule tripped. INDETERMINATE: upsd unreachable, or a needed variable is missing or not a number."""

    OK = "OK"
    ALARM = "ALARM"
    INDETERMINATE = "INDETERMINATE"


class EnumUpsFault(StrEnum):
    """The named break a drill injects; a drill replaces the reading with one that breaks exactly that rule and delivers nothing."""

    NONE = "none"
    #: The status reads as not exactly OL; nothing is delivered.
    ON_BATTERY = "on-battery"
    #: battery.runtime reads below battery.runtime.low; nothing is delivered.
    RUNTIME_BELOW_LOW = "runtime-below-low"
    #: ups.load reads above the overlay's bound; nothing is delivered.
    OVERLOADED = "overloaded"


class ModelUpsCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: False reads without delivering the verdict, so a probe changes nothing.
    write: bool = True
    fault: EnumUpsFault = EnumUpsFault.NONE


class ModelUpsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumUpsOutcome
    fault: EnumUpsFault
    ups_name: str
    alarms: tuple[str, ...]
    #: Why the run is INDETERMINATE, or which rules tripped on an ALARM.
    reasons: tuple[str, ...]
    evidence: str
    failure: str = ""
    delivery: tuple[str, ...] = ()
    #: The readings the run took, one ``name=value`` line each.
    readings: tuple[str, ...] = ()


__all__ = [
    "EnumUpsFault",
    "EnumUpsOutcome",
    "ModelUpsCommand",
    "ModelUpsResult",
]
