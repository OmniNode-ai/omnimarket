# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the lab alarm's broker lag condition (OMN-23024)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumBrokerLagOutcome(StrEnum):
    """OK: no group grew. ALARM: a group grew or its offsets vanished. INDETERMINATE: not read."""

    OK = "OK"
    ALARM = "ALARM"
    INDETERMINATE = "INDETERMINATE"


class EnumLabAlarmFault(StrEnum):
    """The named breaks a drill injects; a run with one never persists its sample."""

    NONE = "none"
    #: The first derived group's lag reads higher than in the previous sample.
    PLANTED_LAG = "planted-lag"
    #: The declared nodes include one the running image does not carry.
    UNKNOWN_NODE = "unknown-node"
    #: The manifest endpoint does not answer (a dev redeploy in progress).
    MANIFEST_DOWN = "manifest-down"


class EnumGroupState(StrEnum):
    READ = "read"
    NEVER_COMMITTED = "never-committed"
    UNREADABLE = "unreadable"


class ModelBrokerLagCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: False reads without keeping the sample, so a probe never moves the baseline.
    write: bool = True
    fault: EnumLabAlarmFault = EnumLabAlarmFault.NONE


class ModelGroupReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    group: str
    state: EnumGroupState
    lag: int | None = None
    previous_lag: int | None = None
    detail: str = ""


class ModelBrokerLagResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumBrokerLagOutcome
    fault: EnumLabAlarmFault
    #: Every group name derived from the running image's contracts, sorted.
    declared_groups: tuple[str, ...]
    readings: tuple[ModelGroupReading, ...]
    #: One line per alarm subject: the group and what it did.
    alarms: tuple[str, ...]
    #: Why a run is INDETERMINATE, or the never-committed notes of an OK one.
    reasons: tuple[str, ...]
    sample_kept: bool
    evidence: str
    failure: str = ""


__all__ = [
    "EnumBrokerLagOutcome",
    "EnumGroupState",
    "EnumLabAlarmFault",
    "ModelBrokerLagCommand",
    "ModelBrokerLagResult",
    "ModelGroupReading",
]
