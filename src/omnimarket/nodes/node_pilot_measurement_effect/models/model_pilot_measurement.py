# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the pilot measurement read: each pilot's outcome, the counts, the run."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingVerdict


class EnumPilotState(StrEnum):
    """One pilot against its frozen bar. There is no extended state."""

    REFUSED = "refused"
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    #: The pilot's result could not be read, was missing, or was stale.
    UNMEASURED = "unmeasured"


class ModelPilotResultRead(BaseModel):
    """One pilot's measured result as a result port read it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics: dict[str, float]
    #: When the result was measured; a result without a time zone is not a result.
    measured_at: AwareDatetime


class ModelPilotOutcome(BaseModel):
    """One pilot's verdict for the run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pilot: str
    state: EnumPilotState
    reasons: tuple[str, ...] = ()


class ModelPilotCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pilots: int = 0
    accepted: int = 0
    rejected: int = 0
    refused: int = 0
    unmeasured: int = 0


class ModelPilotMeasurementCommand(BaseModel):
    """Run the measurement once; ``write`` false reads and appends nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    write: bool = True
    #: Set when the piece upstream of this read failed under this correlation id: the read does
    #: not start, reads nothing and answers with a typed failure that carries this code (the
    #: join's error shape).
    upstream_failure: str | None = Field(default=None, min_length=1)


class ModelPilotMeasurementResult(BaseModel):
    """The run: its verdict, each pilot's outcome, the counts, and what happened on the ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    host: str
    verdict: EnumReadingVerdict | None = None
    #: How the row came to be: measured, refused, rejected or unmeasured.
    outcome: str = ""
    reasons: tuple[str, ...] = ()
    outcomes: tuple[ModelPilotOutcome, ...] = ()
    counts: ModelPilotCounts | None = None
    ledger: str = "not-written"
    row: str = ""
    row_key: str = ""
    failure: str | None = None


__all__ = [
    "EnumPilotState",
    "ModelPilotCounts",
    "ModelPilotMeasurementCommand",
    "ModelPilotMeasurementResult",
    "ModelPilotOutcome",
    "ModelPilotResultRead",
]
