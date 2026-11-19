# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the lab alarm's lab-pass-receipt condition."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumLabPassReceiptOutcome(StrEnum):
    """OK: the lane's receipt is a passing one. ALARM: the receipt is a failing one. INDETERMINATE: the sha or the receipt is unreadable, absent, malformed or stale."""

    OK = "OK"
    ALARM = "ALARM"
    INDETERMINATE = "INDETERMINATE"


class EnumLabPassReceiptFault(StrEnum):
    """The named breaks a drill injects; a drill keeps no state and delivers nothing."""

    NONE = "none"
    #: An injected failed receipt reads ALARM without reading, delivering or recording.
    PLANTED_FAIL = "planted-fail"


class ModelLabPassReceiptCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: False reads without delivering the verdict, so a probe changes nothing.
    write: bool = True
    fault: EnumLabPassReceiptFault = EnumLabPassReceiptFault.NONE


class ModelLabPassCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    ok: bool


class ModelLabPassReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    lane: str = Field(min_length=1)
    finished_at: datetime
    result: Literal["PASS", "FAIL"]
    checks: tuple[ModelLabPassCheck, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _verdict_matches_evidence(self) -> ModelLabPassReceipt:
        all_ok = all(check.ok for check in self.checks)
        if self.result == "PASS" and not all_ok:
            raise ValueError("a PASS receipt must have every check ok")
        if self.result == "FAIL" and all_ok:
            raise ValueError("a FAIL receipt must have at least one failing check")
        return self


class ModelLabPassReceiptResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumLabPassReceiptOutcome
    fault: EnumLabPassReceiptFault
    sha: str
    surface: str
    readings: tuple[str, ...]
    alarms: tuple[str, ...]
    #: Why the run is INDETERMINATE, or which repositories are still waiting on their streak.
    reasons: tuple[str, ...]
    evidence: str
    failure: str = ""
    delivery: tuple[str, ...] = ()


__all__ = [
    "EnumLabPassReceiptFault",
    "EnumLabPassReceiptOutcome",
    "ModelLabPassCheck",
    "ModelLabPassReceipt",
    "ModelLabPassReceiptCommand",
    "ModelLabPassReceiptResult",
]
