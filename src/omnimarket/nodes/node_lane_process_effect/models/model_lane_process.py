# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the lane process effect: one operation over a spi start spec."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from omnibase_spi.contracts.services import ModelLaneStartSpec
from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumLaneProcessOperation(StrEnum):
    """The two operations of ProtocolLaneProcessRunner."""

    START_DETACHED = "start_detached"
    RUN_BOUNDED = "run_bounded"


class EnumLaneProcessOutcome(StrEnum):
    """What the effect answers: the runner's outcomes, or its own refusal."""

    STARTED = "started"
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    REFUSED = "refused"


class ModelLaneProcessRequest(BaseModel):
    """One operation over one spec; the orchestrator builds every value from its overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumLaneProcessOperation
    spec: ModelLaneStartSpec
    # The deadline of run_bounded; a start_detached request carries none.
    timeout_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _deadline_follows_operation(self) -> Self:
        if self.operation is EnumLaneProcessOperation.RUN_BOUNDED:
            if self.timeout_seconds is None:
                raise ValueError("run_bounded requires timeout_seconds")
        elif self.timeout_seconds is not None:
            raise ValueError("start_detached carries no timeout_seconds")
        return self


class ModelLaneProcessResult(BaseModel):
    """The outcome of one operation; the fields that do not belong to the outcome are empty."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumLaneProcessOperation
    outcome: EnumLaneProcessOutcome
    pid: int | None = None
    exit_code: int | None = None
    stdout: str = ""
    cause: str | None = None
