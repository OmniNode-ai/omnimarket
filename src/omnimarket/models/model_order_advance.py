# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the order writer (rebuild plan section 7.5, the holder).

The holder is the record of each piece's state (started, live, in its comparison, kept or rolled
back) and one order writer on a timer. The writer reads the gate store's order position and the
piece records, makes at most one change per piece per tick, and advances the gate store's order
only when the piece at the order position is kept. Every write carries a step key
(``run_key/...``) and a fence token; a replayed key returns the first answer and writes nothing,
and a token older than the newest one the record has seen is refused.

No value of the policy lives here: the backend and the comparison window arrive from the
``order_advance`` overlay.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumRunEngineBackend(StrEnum):
    """Which backend the overlay selects (``run_engine_backend``); only the timer store is built."""

    TIMER_STORE = "timer_store"
    TEMPORAL_SELF_HOSTED = "temporal_self_hosted"


class EnumPieceState(StrEnum):
    """The states of a piece, in the order it passes them."""

    STARTED = "started"
    LIVE = "live"
    IN_COMPARISON = "in_comparison"
    KEPT = "kept"
    ROLLED_BACK = "rolled_back"


class EnumComparisonReading(StrEnum):
    """What the comparison of a live piece read."""

    KEPT = "kept"
    ROLLED_BACK = "rolled_back"


class EnumPieceRecordOperation(StrEnum):
    """The verbs the piece records answer."""

    WRITE = "write"
    ADOPT = "adopt"
    READ = "read"


class EnumPieceRecordOutcome(StrEnum):
    """The typed answer of one record request."""

    OK = "ok"
    DUPLICATE = "duplicate"
    REFUSED_FENCE = "refused_fence"
    REFUSED_TRANSITION = "refused_transition"
    REFUSED_INVALID = "refused_invalid"


class EnumOrderAdvanceOperation(StrEnum):
    """What a request asks the writer to do."""

    TICK = "tick"
    TAKE_OVER = "take_over"


class EnumOrderAdvanceOutcome(StrEnum):
    """The typed answer of one order-writer request."""

    ADVANCED = "advanced"
    IDLE = "idle"
    COMPLETE = "complete"
    TAKEN_OVER = "taken_over"
    REFUSED_NOT_HOLDER = "refused_not_holder"
    REFUSED_FENCE = "refused_fence"
    REFUSED_PLACEMENT = "refused_placement"
    REFUSED_INVALID = "refused_invalid"


class ModelOrderAdvancePolicy(BaseModel):
    """The ``order_advance`` overlay: every setting of the writer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_engine_backend: EnumRunEngineBackend
    comparison_seconds: int = Field(ge=1)


class ModelPieceEvidence(BaseModel):
    """What the caller read about the piece at the order position; the writer reads nothing itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tests_pass: bool = False
    joins_pass: bool = False
    readback_pass: bool = False
    comparison: EnumComparisonReading | None = None


class ModelPieceRecord(BaseModel):
    """One piece's record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    piece: str
    state: EnumPieceState
    live_at: float | None = None
    updated_at: float = 0.0


class ModelPieceRecordRequest(BaseModel):
    """One request to the piece records; ``now`` is read by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumPieceRecordOperation
    step_key: str | None = None
    now: float = 0.0
    piece: str | None = None
    to_state: EnumPieceState | None = None
    holder: str | None = None
    fence_token: int | None = None


class ModelPieceRecordResult(BaseModel):
    """The answer of one record request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumPieceRecordOperation
    outcome: EnumPieceRecordOutcome
    record: ModelPieceRecord | None = None
    high_water: int = 0
    holder_token: int | None = None
    detail: str = ""


class ModelOrderAdvanceRequest(BaseModel):
    """One tick (or one take-over) of one writer; ``now`` is read by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    operation: EnumOrderAdvanceOperation
    run_key: str
    writer_id: str = Field(pattern=r"^[^/\s]+$")
    host: str = Field(pattern=r"^[^/\s]+$")
    now: float = 0.0
    evidence: ModelPieceEvidence = ModelPieceEvidence()
    expected_epoch: int | None = None


class ModelOrderAdvanceResult(BaseModel):
    """The answer of one request, whatever happened."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    operation: EnumOrderAdvanceOperation
    outcome: EnumOrderAdvanceOutcome
    epoch: int = 0
    order_position: int = 0
    holder: str | None = None
    piece: str | None = None
    piece_state: EnumPieceState | None = None
    detail: str = ""


class ModelOrderAdvanceFailure(BaseModel):
    """The typed failure event of a command that is no request, or a store that could not answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    failure: str


def holder_id(host: str, writer_id: str) -> str:
    """The holder string the gate store records: the host first, so placement reads it back."""
    return f"{host}/{writer_id}"


def holder_host(holder: str) -> str:
    """The host a holder string names."""
    return holder.split("/", 1)[0]
