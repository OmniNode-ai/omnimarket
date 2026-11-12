# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the lab alarm's container-restarts condition."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumContainerRestartsOutcome(StrEnum):
    """OK: every container's restart count is within its bound. ALARM: a container restarted past its bound. INDETERMINATE: a container or the lane's bounds could not be read."""

    OK = "OK"
    ALARM = "ALARM"
    INDETERMINATE = "INDETERMINATE"


class EnumContainerRestartsFault(StrEnum):
    """The named breaks a drill injects; a drill keeps no state and delivers nothing."""

    NONE = "none"
    #: The first declared container, sorted by name, reads as one restart past its bound, status
    #: ``running``; nothing is read from docker and nothing is delivered.
    RESTARTS_OVER_BOUND = "restarts-over-bound"


class ModelContainerRestartsCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: False reads without delivering the verdict, so a probe changes nothing.
    write: bool = True
    fault: EnumContainerRestartsFault = EnumContainerRestartsFault.NONE


class ModelContainerRestartsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumContainerRestartsOutcome
    fault: EnumContainerRestartsFault
    #: The lane whose containers this run read, the overlay's ``container_restarts.census_lane``.
    census_lane: str
    alarms: tuple[str, ...]
    #: What each container read, the containers sorted by name as the old script walked them.
    readings: tuple[str, ...]
    #: Why the run is INDETERMINATE, or which containers restarted past their bound on an ALARM.
    reasons: tuple[str, ...]
    evidence: str
    failure: str = ""
    delivery: tuple[str, ...] = ()


__all__ = [
    "EnumContainerRestartsFault",
    "EnumContainerRestartsOutcome",
    "ModelContainerRestartsCommand",
    "ModelContainerRestartsResult",
]
