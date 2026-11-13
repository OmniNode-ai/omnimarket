# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the lab alarm's broker-listener condition."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumBrokerListenerOutcome(StrEnum):
    """OK: an address answered. ALARM: every address refused, timed out or closed. INDETERMINATE: a name cannot be resolved, or the bootstrap string is not host:port[,host:port]."""

    OK = "OK"
    ALARM = "ALARM"
    INDETERMINATE = "INDETERMINATE"


class EnumBrokerListenerFault(StrEnum):
    """The named breaks a drill injects; a drill keeps no state and delivers nothing."""

    NONE = "none"
    #: Every address reads as refused; nothing is delivered.
    LISTENER_REFUSED = "listener-refused"


class ModelBrokerListenerCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: False reads without delivering the verdict, so a probe changes nothing.
    write: bool = True
    fault: EnumBrokerListenerFault = EnumBrokerListenerFault.NONE


class ModelBrokerListenerResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumBrokerListenerOutcome
    fault: EnumBrokerListenerFault
    bootstrap_servers: str
    alarms: tuple[str, ...]
    #: Why the run is INDETERMINATE, or which addresses answered or refused on an ALARM.
    reasons: tuple[str, ...]
    evidence: str
    failure: str = ""
    delivery: tuple[str, ...] = ()


__all__ = [
    "EnumBrokerListenerFault",
    "EnumBrokerListenerOutcome",
    "ModelBrokerListenerCommand",
    "ModelBrokerListenerResult",
]
