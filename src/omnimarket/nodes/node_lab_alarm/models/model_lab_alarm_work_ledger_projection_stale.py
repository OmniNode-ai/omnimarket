# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the lab alarm's work-ledger-projection-stale condition."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumWorkLedgerProjectionStaleOutcome(StrEnum):
    """OK: the projection is inside its bound of the ledger file. ALARM: it trails the file by more than the bound, or holds no row in the window. INDETERMINATE: the ledger file or the projection cannot be read."""

    OK = "OK"
    ALARM = "ALARM"
    INDETERMINATE = "INDETERMINATE"


class EnumWorkLedgerProjectionStaleFault(StrEnum):
    """The named breaks a drill injects; a drill keeps no state and delivers nothing."""

    NONE = "none"
    #: The first subject reads as an injected breach that bypasses persistence; nothing is read, kept or delivered.
    PROJECTION_BEHIND = "projection-behind"


class ModelWorkLedgerProjectionStaleCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: False reads without delivering the verdict, so a probe changes nothing.
    write: bool = True
    fault: EnumWorkLedgerProjectionStaleFault = EnumWorkLedgerProjectionStaleFault.NONE


class ModelWorkLedgerProjectionStaleResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumWorkLedgerProjectionStaleOutcome
    fault: EnumWorkLedgerProjectionStaleFault
    subject: str
    readings: tuple[str, ...]
    alarms: tuple[str, ...]
    #: Why the run is INDETERMINATE, or which subjects are still waiting on their streak.
    reasons: tuple[str, ...]
    evidence: str
    failure: str = ""
    delivery: tuple[str, ...] = ()


__all__ = [
    "EnumWorkLedgerProjectionStaleFault",
    "EnumWorkLedgerProjectionStaleOutcome",
    "ModelWorkLedgerProjectionStaleCommand",
    "ModelWorkLedgerProjectionStaleResult",
]
