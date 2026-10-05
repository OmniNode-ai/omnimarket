# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input, output, transition and intent models of ``node_lab_job_reducer``."""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_lab_job import (
    EnumLabJobIntentKind,
    EnumLabJobState,
)
from omnimarket.models.lab_job.model_lab_job_event import ModelLabJobEvent
from omnimarket.models.lab_job.model_lab_job_row import ModelLabJobRow


class ModelLabJobIntent(BaseModel):
    """A typed intent. ``dispatch`` carries the brief built from the original one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumLabJobIntentKind
    job_id: str
    attempt: int = Field(ge=1)
    work_unit_id: str | None = None
    brief: str | None = None
    runtime_id: str | None = None
    dedup_key: str | None = None
    reason: str | None = None


class ModelLabJobTransition(BaseModel):
    """One state change, for the projection's transitions table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    seq: int = Field(ge=1)
    from_state: EnumLabJobState | None
    to_state: EnumLabJobState
    attempt: int = Field(ge=1)
    at: datetime
    reason: str


class ModelLabJobReduceInput(BaseModel):
    """The stored row (None on first sight of the job) and one event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelLabJobRow | None
    event: ModelLabJobEvent

    @model_validator(mode="after")
    def _same_job(self) -> Self:
        if self.row is not None and self.row.job_id != self.event.job_id:
            raise ValueError("row and event name different jobs")
        return self


class ModelLabJobReduceOutput(BaseModel):
    """The next row, the transitions taken (or why the event dropped), the intents."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelLabJobRow | None
    applied: bool
    drop_reason: str | None = None
    transitions: tuple[ModelLabJobTransition, ...] = ()
    intents: tuple[ModelLabJobIntent, ...] = ()

    @model_validator(mode="after")
    def _drop_has_reason(self) -> Self:
        if not self.applied and not self.drop_reason:
            raise ValueError("a dropped event needs a drop_reason")
        if not self.applied and (
            self.transitions
            or any(
                i.kind is not EnumLabJobIntentKind.TAKE_REFUSED for i in self.intents
            )
        ):
            raise ValueError("a dropped event carries only take_refused intents")
        return self


__all__: list[str] = [
    "ModelLabJobIntent",
    "ModelLabJobReduceInput",
    "ModelLabJobReduceOutput",
    "ModelLabJobTransition",
]
