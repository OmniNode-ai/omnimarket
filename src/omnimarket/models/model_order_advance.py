# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the order writer (rebuild plan section 7.5, step 0 row 3).

The order writer moves the order position of the gate store one step at a time, on a timer,
as the holder at the current epoch. It holds no start: its loss refuses none. No value of its
policy lives here; the holder name, the step-key prefix, the readiness backend and the ready
pieces arrive from the ``order_writer`` overlay.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumOrderReadinessBackend(StrEnum):
    """Where the writer reads "this piece is ready"; the floor reading is not built yet."""

    IN_MEMORY = "in_memory"
    FLOOR_READING = "floor_reading"


class EnumOrderAdvanceOutcome(StrEnum):
    """The typed answer of one tick."""

    ADVANCED = "advanced"
    WAITING = "waiting"
    COMPLETE = "complete"
    FENCED = "fenced"
    HANDOVER_REFUSED = "handover_refused"
    REFUSED = "refused"


class ModelOrderWriterPolicy(BaseModel):
    """The ``order_writer`` overlay: every setting of the writer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    holder: str = Field(min_length=1)
    run_key: str = Field(min_length=1)
    run_engine_backend: str = Field(min_length=1)
    readiness_backend: EnumOrderReadinessBackend
    ready_pieces: tuple[str, ...] = ()
    takes_over_from_other_holder: bool


class ModelOrderAdvanceRequest(BaseModel):
    """One tick; the time is read by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    now: float = 0.0


class ModelOrderAdvanceResult(BaseModel):
    """What one tick did, whatever happened."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumOrderAdvanceOutcome
    order_position: int
    epoch: int
    holder: str | None = None
    next_piece: str | None = None
    detail: str = ""


class ModelOrderAdvanceFailure(BaseModel):
    """The typed failure event of a tick the writer could not answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    failure: str
