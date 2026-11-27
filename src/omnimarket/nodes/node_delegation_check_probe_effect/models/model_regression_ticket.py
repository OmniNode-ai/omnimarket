# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input, output and per-check state of the red-check ticket handler (OMN-26726)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    EnumCheckVerdict,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumRegressionAction(StrEnum):
    #: Nothing to write: green with no open ticket, or a run that measured nothing.
    NONE = "none"
    OPENED = "opened"
    COMMENTED = "commented"
    RECOVERY_COMMENTED = "recovery-commented"
    #: The ticket node refused or was unreachable; the next red run tries again.
    FAILED = "failed"


class ModelRegressionRunReading(_Frozen):
    """What one scheduled run of one check read."""

    check_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    verdict: EnumCheckVerdict
    #: The first failed clause (or the error) of the run; blank for a green run.
    failing_step: str = ""


class ModelRegressionTicketRequest(_Frozen):
    reading: ModelRegressionRunReading


class ModelRegressionTicketResult(_Frozen):
    check_id: str
    run_id: str
    action: EnumRegressionAction
    ticket_id: str = ""
    detail: str = ""


class ModelRegressionCheckState(_Frozen):
    """What the next run needs to know about one check: its last reading, last green run and open ticket."""

    last_verdict: str = ""  # "green", "red" or "" before any reading
    last_green_run: str = ""
    last_red_run: str = ""
    open_ticket: str = ""
    recovery_noted: bool = False


__all__ = [
    "EnumRegressionAction",
    "ModelRegressionCheckState",
    "ModelRegressionRunReading",
    "ModelRegressionTicketRequest",
    "ModelRegressionTicketResult",
]
