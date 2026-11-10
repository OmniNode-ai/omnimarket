# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed command, crossing and result of the seam deadline probe (piece 3 of the base-first plan)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.models.seam_reading.model_required_sender import ModelSeamCounts
from omnimarket.models.seam_reading.model_seam_demand import (
    EnumSeamCrossing,
    ModelSeamDemand,
)


class EnumSeamFault(StrEnum):
    """The fault a drill injects; ``none`` is an operational run.

    ``unanswerable-command`` sends each seam's overlay ``fault_request`` in place of its request: a
    command the consuming node answers with no success result, so no result arrives by the deadline
    (the plan's named break for piece 3). ``deadline-zero`` waits no time at all for the result.
    """

    NONE = "none"
    UNANSWERABLE_COMMAND = "unanswerable-command"
    DEADLINE_ZERO = "deadline-zero"


class ModelSeamDeadlineCommand(BaseModel):
    """Run every seam the overlay declares, or the ones named."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    seams: tuple[str, ...] = ()
    write: bool = False
    fault: EnumSeamFault = EnumSeamFault.NONE


class ModelSeamAnswer(BaseModel):
    """What the bus saw for one demand. ``detail`` is a cell-safe token, never answer content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    crossing: EnumSeamCrossing
    elapsed_ms: int | None = Field(default=None, ge=0)
    detail: str = ""
    #: Ms from the start of the send to the broker acknowledging it.
    send_ack_ms: int | None = Field(default=None, ge=0)
    #: Ms from the start of the send to the correlated reply arriving after the deadline, inside
    #: the listen window.
    late_reply_ms: int | None = Field(default=None, ge=0)
    #: The reply envelope's own creation time minus the wall time the send began, in ms (two hosts'
    #: clocks, so it carries their skew; may be negative).
    reply_stamp_ms: int | None = None


class ModelSeamReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seam: str
    verdict: EnumReadingVerdict
    crossing: EnumSeamCrossing
    elapsed_ms: int | None = None
    deadline_seconds: float
    correlation_id: str
    detail: str = ""
    #: Ms from the start of the send to the broker acknowledging it.
    send_ack_ms: int | None = Field(default=None, ge=0)
    #: Ms from the start of the send to the correlated reply arriving after the deadline, inside
    #: the listen window.
    late_reply_ms: int | None = Field(default=None, ge=0)
    #: The reply envelope's own creation time minus the wall time the send began, in ms (two hosts'
    #: clocks, so it carries their skew; may be negative).
    reply_stamp_ms: int | None = None
    #: How long past the deadline the probe kept reading for a late reply; 0 listened no longer.
    late_listen_seconds: float = Field(default=0.0, ge=0)


class ModelSeamDeadlineResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    verdict: EnumReadingVerdict
    outcome: EnumReadingOutcome
    reasons: tuple[str, ...]
    fault: EnumSeamFault
    seams: tuple[ModelSeamReading, ...] = ()
    failure: str | None = None
    #: The seam counts of the overlay's declarations; None when it declares none.
    counts: ModelSeamCounts | None = None
    row_key: str | None = None
    ledger: str = "not-written"
    row: str | None = None


__all__ = [
    "EnumSeamCrossing",
    "EnumSeamFault",
    "ModelSeamAnswer",
    "ModelSeamDeadlineCommand",
    "ModelSeamDeadlineResult",
    "ModelSeamDemand",
    "ModelSeamReading",
]
