# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the gate store (rebuild plan section 7.5, step 0 row 3).

One store holds the epoch, the reservations, the consumed reservations, the holder record,
the step keys, the stop and safety state and the order. Every compare is evaluated inside
one transaction of the store, never from a projection. Every mutating request carries a step
key (``run_key/step``); a replayed key returns the first recorded answer and writes nothing.

No value of the store's policy lives here: the limits, the reservation lifetime, the order
and the backend arrive from the ``gate_store`` overlay.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumGateStoreBackend(StrEnum):
    """Which backend the overlay selects; the in-memory twin is the CI default."""

    IN_MEMORY = "in_memory"
    POSTGRES = "postgres"


class EnumGateStoreOperation(StrEnum):
    """The verbs the store answers."""

    RESERVE = "reserve"
    CONSUME = "consume"
    RELEASE = "release"
    RECORD_SAFETY = "record_safety"
    HANDOVER = "handover"
    ADVANCE_ORDER = "advance_order"
    READ = "read"


class EnumGateStoreOutcome(StrEnum):
    """The typed answer of one request."""

    OK = "ok"
    DUPLICATE = "duplicate"
    REFUSED_LIMIT = "refused_limit"
    REFUSED_STOP = "refused_stop"
    REFUSED_UNKNOWN_PIECE = "refused_unknown_piece"
    REFUSED_UNKNOWN_RESERVATION = "refused_unknown_reservation"
    REFUSED_EXPIRED = "refused_expired"
    REFUSED_CONSUMED = "refused_consumed"
    REFUSED_PROCESS_LIVE = "refused_process_live"
    REFUSED_FENCE = "refused_fence"
    REFUSED_ORDER = "refused_order"
    REFUSED_INVALID = "refused_invalid"


class ModelGateStorePolicy(BaseModel):
    """The ``gate_store`` overlay: every setting of the store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumGateStoreBackend
    limits: dict[str, int]
    reservation_ttl_seconds: int = Field(ge=1)
    order: tuple[str, ...]


class ModelGateStoreRequest(BaseModel):
    """One request; ``now`` is read by the caller, never by the store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    operation: EnumGateStoreOperation
    step_key: str | None = None
    now: float = 0.0
    piece: str | None = None
    reservation_id: str | None = None
    process_gone: bool = False
    safety_state: str | None = None
    holder: str | None = None
    expected_epoch: int | None = None
    order_position: int | None = None


class ModelGateStoreResult(BaseModel):
    """The answer of one request, whatever happened."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    operation: EnumGateStoreOperation
    outcome: EnumGateStoreOutcome
    epoch: int
    counted: int = 0
    holder: str | None = None
    order_position: int = 0
    detail: str = ""


class ModelGateStoreFailure(BaseModel):
    """The typed failure event of a request the store could not answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    failure: str
