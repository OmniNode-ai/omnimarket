# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire models of one call to the declared ``kind: harness`` backends (OMN-26219).

A harness call walks the escalation chain the delegation routing overlay declares for
its task class: each admitted harness rung is one attempt, each attempt is judged by
the delegation answer gate, and each attempt leaves one cost record. Which backends
exist, their models and their order are overlay facts; nothing here names one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_harness_rung_refusal import EnumHarnessRungRefusal
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendSurface,
)

HarnessExitCause = Literal[
    "completed",
    "timeout",
    "launch_failed",
    "process_failed",
    "startup_refused",
    "turn_failed",
    "invalid_output",
    "api_key_login_refused",
]


class ModelHarnessBackendCallRequest(BaseModel):
    """One delegation a caller sends to the harness handler."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    request_id: UUID
    task_class: str = Field(min_length=1)
    prompt: str = Field(min_length=1, repr=False)
    tenant_slug: str | None = None
    surface: EnumDelegationBackendSurface
    pinned_backend_id: str | None = Field(
        default=None,
        description="The one backend id a caller pins; the only way to reach a pin-only rung.",
    )
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    caller_lane: str | None = None


class ModelHarnessRunReceipt(BaseModel):
    """What one harness process reported; a missing measurement is null, never zero."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str = Field(default="", repr=False)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    wall_seconds: float = Field(ge=0, allow_inf_nan=False)
    exit_cause: HarnessExitCause
    exit_code: int | None = None


class ModelHarnessGateVerdict(BaseModel):
    """The answer gate's decision on one attempt's answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    reasons: tuple[str, ...] = ()
    gate_kind: str = Field(min_length=1)


class ModelHarnessAttemptRecord(BaseModel):
    """One attempt: the rung that ran, what it cost and what the gate said.

    One of these is written per attempt, accepted or not; it is the cost record the
    delegation events row for the call is joined to.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    attempt: int = Field(ge=1)
    tier: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    harness: str = Field(min_length=1)
    model_name: str | None = None
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    wall_seconds: float = Field(ge=0, allow_inf_nan=False)
    exit_cause: str
    accepted: bool
    gate_kind: str | None = None
    gate_reasons: tuple[str, ...] = ()
    caller_lane: str | None = None


class ModelHarnessRungRefusal(BaseModel):
    """A declared rung the call was not admitted to, with the reason for each refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: str
    backend_id: str | None
    refusals: tuple[EnumHarnessRungRefusal, ...]


class ModelHarnessBackendCallResult(BaseModel):
    """The call's outcome; ``attempts`` is empty only when nothing was admitted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    # ``completed`` is the runtime's own success word for a terminal; a call with no
    # accepted answer states ``ok=False`` so the terminal verdict reads it as a failure.
    status: Literal["completed", "rejected", "refused"]
    ok: bool
    content: str = Field(default="", repr=False)
    backend_id: str | None = None
    model_name: str | None = None
    attempts: tuple[ModelHarnessAttemptRecord, ...] = ()
    refused_rungs: tuple[ModelHarnessRungRefusal, ...] = ()
    detail: str = ""


__all__ = [
    "HarnessExitCause",
    "ModelHarnessAttemptRecord",
    "ModelHarnessBackendCallRequest",
    "ModelHarnessBackendCallResult",
    "ModelHarnessGateVerdict",
    "ModelHarnessRunReceipt",
    "ModelHarnessRungRefusal",
]
