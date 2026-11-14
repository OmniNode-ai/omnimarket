# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the lab alarm's delegation-chain-canary condition."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumDelegationChainCanaryOutcome(StrEnum):
    """OK: the newest completed run is green and fresh. ALARM: the newest completed run is red or stale. INDETERMINATE: the source is unreadable."""

    OK = "OK"
    ALARM = "ALARM"
    INDETERMINATE = "INDETERMINATE"


class EnumDelegationChainCanaryFault(StrEnum):
    """The named breaks a drill injects; a drill keeps no state and delivers nothing."""

    NONE = "none"
    #: The first subject reads as an injected breach that bypasses persistence; nothing is read, kept or delivered.
    RED_CANARY = "red-canary"


class ModelCanaryRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    event: str
    status: str | None
    conclusion: str | None
    created_at: datetime
    html_url: str = ""


class ModelDelegationChainCanaryCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: False reads without delivering the verdict, so a probe changes nothing.
    write: bool = True
    fault: EnumDelegationChainCanaryFault = EnumDelegationChainCanaryFault.NONE


class ModelDelegationChainCanaryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumDelegationChainCanaryOutcome
    fault: EnumDelegationChainCanaryFault
    subject: str
    readings: tuple[str, ...]
    alarms: tuple[str, ...]
    #: Why the run is INDETERMINATE, or which subjects are still waiting on their streak.
    reasons: tuple[str, ...]
    evidence: str
    failure: str = ""
    delivery: tuple[str, ...] = ()


__all__ = [
    "EnumDelegationChainCanaryFault",
    "EnumDelegationChainCanaryOutcome",
    "ModelCanaryRun",
    "ModelDelegationChainCanaryCommand",
    "ModelDelegationChainCanaryResult",
]
