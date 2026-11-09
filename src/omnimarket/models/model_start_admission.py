# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the start-admission effect (rebuild plan sections 4.1, 7.1 and W28).

Admission sits at each start path's last step before an engine, session or provisioning call. It
reserves in the gate store, consumes the reservation once, and answers whether the start proceeds.
In ``observe`` mode it only logs what it would refuse and the start proceeds; in ``enforce`` mode a
refusal stops the start. A path stays in ``observe`` until the drill passes on it.

No value of the policy lives here: the mode of each path, the answer deadline and the number of
re-decisions arrive from the ``start_admission`` overlay.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumAdmissionMode(StrEnum):
    """Whether a refusal stops the start (enforce) or is only logged (observe)."""

    OBSERVE = "observe"
    ENFORCE = "enforce"


class EnumAdmissionOutcome(StrEnum):
    """The typed answer of one admission request."""

    ADMITTED = "admitted"
    OBSERVED_REFUSAL = "observed_refusal"
    REFUSED = "refused"
    UNADMITTED = "unadmitted"
    NOT_PROCEEDED = "not_proceeded"


class ModelStartAdmissionPolicy(BaseModel):
    """The ``start_admission`` overlay: every setting of the effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_mode: EnumAdmissionMode
    modes: dict[str, EnumAdmissionMode]
    answer_deadline_seconds: float = Field(gt=0)
    max_redecisions: int = Field(ge=0)


class ModelStartAdmissionRequest(BaseModel):
    """One start path asking to start; ``now`` is read by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    step_key: str
    path: str
    piece: str
    reservation_id: str
    outward: bool = False
    now: float = 0.0


class ModelStartAdmissionLogRow(BaseModel):
    """What the effect decided, one row per request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    step_key: str
    path: str
    piece: str
    mode: EnumAdmissionMode
    outcome: EnumAdmissionOutcome
    proceed: bool
    reason: str = ""
    epoch: int | None = None
    attempts: int = 0


class ModelStartAdmissionResult(BaseModel):
    """The answer of one admission request, whatever happened."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    path: str
    mode: EnumAdmissionMode
    outcome: EnumAdmissionOutcome
    proceed: bool
    reason: str = ""
    epoch: int | None = None
    attempts: int = 0


class ModelStartAdmissionFailure(BaseModel):
    """The typed failure event of a command that is no request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    failure: str
