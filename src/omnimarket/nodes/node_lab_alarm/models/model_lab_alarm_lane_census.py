# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the lab alarm's lane-census conditions."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumLaneCensusCondition(StrEnum):
    """The two lane-census conditions: lane drift and container health."""

    LANE_DRIFT = "lane_drift"
    CONTAINER_UNHEALTHY = "container_unhealthy"


class EnumLaneCensusOutcome(StrEnum):
    """OK: no findings of the condition. ALARM: at least one finding. INDETERMINATE: the snapshot is unreadable, invalid, not a census event, has a mismatched alert key, or is stale."""

    OK = "OK"
    ALARM = "ALARM"
    INDETERMINATE = "INDETERMINATE"


class EnumLaneCensusFault(StrEnum):
    """The named breaks a drill injects; a drill keeps no state and delivers nothing."""

    NONE = "none"
    #: One injected finding makes the condition read ALARM without reading, delivering or recording.
    PLANTED_FINDING = "planted-finding"


class ModelLaneCensusCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: False reads without delivering the verdict, so a probe changes nothing.
    write: bool = True
    fault: EnumLaneCensusFault = EnumLaneCensusFault.NONE


class ModelLaneCensusResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    condition: EnumLaneCensusCondition
    outcome: EnumLaneCensusOutcome
    fault: EnumLaneCensusFault
    host: str
    readings: tuple[str, ...]
    alarms: tuple[str, ...]
    reasons: tuple[str, ...]
    evidence: str
    failure: str = ""
    delivery: tuple[str, ...] = ()


__all__ = [
    "EnumLaneCensusCondition",
    "EnumLaneCensusFault",
    "EnumLaneCensusOutcome",
    "ModelLaneCensusCommand",
    "ModelLaneCensusResult",
]
