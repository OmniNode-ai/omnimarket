# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the start admission effect (rebuild plan sections 6.3 and 7.5, step 0 row 4).

Admission sits at each start path's last step before an engine, session or provisioning call.
``admit`` takes a reservation on the gate store; ``start`` consumes it (single use), so a start that
never took one is a bypass and reads as one. Every path is in observe (log what would be refused,
admit) or enforce (refuse for real) from the ``start_admission`` overlay; a path that is not in the
overlay is in observe. No path name, mode or log backend lives here.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumAdmissionOperation(StrEnum):
    """The two verbs a start path calls."""

    ADMIT = "admit"
    START = "start"


class EnumEnforcement(StrEnum):
    """Whether a path refuses for real or only logs what it would refuse."""

    OBSERVE = "observe"
    ENFORCE = "enforce"


class EnumAdmissionLogBackend(StrEnum):
    """Where the observe log is kept; the ledger row writer is not built yet."""

    IN_MEMORY = "in_memory"
    LEDGER = "ledger"


class EnumAdmissionDecision(StrEnum):
    """What the path is told."""

    ADMITTED = "admitted"
    ADMITTED_OBSERVED = "admitted_observed"
    ADMITTED_NO_ANSWER = "admitted_no_answer"
    REFUSED = "refused"


class ModelStartAdmissionPolicy(BaseModel):
    """The ``start_admission`` overlay: every setting of admission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enforcement: dict[str, EnumEnforcement]
    log_backend: EnumAdmissionLogBackend


class ModelStartAdmissionRequest(BaseModel):
    """One call from a start path; the time is read by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    operation: EnumAdmissionOperation
    path: str = Field(min_length=1)
    start_id: str = Field(min_length=1)
    piece: str | None = None
    now: float = 0.0


class ModelStartAdmissionResult(BaseModel):
    """What admission told the path, and what it would have done."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    operation: EnumAdmissionOperation
    decision: EnumAdmissionDecision
    path: str
    start_id: str
    enforcement: EnumEnforcement
    path_listed: bool
    would_refuse: bool
    store_outcome: str | None = None
    detail: str = ""


class ModelAdmissionLogEntry(BaseModel):
    """One observe-log record: what was decided against the real state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    operation: EnumAdmissionOperation
    path: str
    start_id: str
    decision: EnumAdmissionDecision
    would_refuse: bool
    store_outcome: str | None
    detail: str


class ModelStartAdmissionFailure(BaseModel):
    """The typed failure event of a call admission could not read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    failure: str
