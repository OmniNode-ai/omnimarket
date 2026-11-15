# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the lab alarm's dev-tag-heartbeat condition."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumDevTagHeartbeatOutcome(StrEnum):
    """OK: every repository's beat is fresh and unalarmed. ALARM: a repository's streak reached the bound. INDETERMINATE: the source is unreadable, has no heartbeat object, or a beat is malformed."""

    OK = "OK"
    ALARM = "ALARM"
    INDETERMINATE = "INDETERMINATE"


class EnumDevTagHeartbeatFault(StrEnum):
    """The named breaks a drill injects; a drill keeps no state and delivers nothing."""

    NONE = "none"
    #: The first repository reads as an injected breach that bypasses persistence; nothing is read, kept or delivered.
    STALE_BEAT = "stale-beat"


class ModelDevTagHeartbeatCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: False reads without delivering the verdict, so a probe changes nothing.
    write: bool = True
    fault: EnumDevTagHeartbeatFault = EnumDevTagHeartbeatFault.NONE


class ModelDevTagHeartbeatResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumDevTagHeartbeatOutcome
    fault: EnumDevTagHeartbeatFault
    repositories: tuple[str, ...]
    readings: tuple[str, ...]
    alarms: tuple[str, ...]
    #: Why the run is INDETERMINATE, or which repositories are still waiting on their streak.
    reasons: tuple[str, ...]
    evidence: str
    failure: str = ""
    delivery: tuple[str, ...] = ()


__all__ = [
    "EnumDevTagHeartbeatFault",
    "EnumDevTagHeartbeatOutcome",
    "ModelDevTagHeartbeatCommand",
    "ModelDevTagHeartbeatResult",
]
