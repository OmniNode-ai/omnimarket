# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelLocalSupervisorResult — output from HandlerLocalSupervisor.handle()."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumSupervisorVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    ESCALATE = "ESCALATE"


class ModelLocalSupervisorResult(BaseModel):
    """Result of local supervisor execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output: str = Field(
        default="",
        description="Model output text on success; empty string on escalation.",
    )
    verdict: EnumSupervisorVerdict = Field(
        ...,
        description="PASS on accepted output, FAIL on single attempt failure, ESCALATE when budget exhausted.",
    )
    attempt_count: int = Field(
        ..., ge=1, description="Number of attempts made before reaching verdict."
    )
    model_key: str = Field(
        ...,
        description="Model key that produced the accepted output (or last attempted key).",
    )
    escalated: bool = Field(
        default=False,
        description="True when the retry budget was exhausted without a verified PASS.",
    )
    correlation_id: str = Field(..., description="Echoed from the originating request.")


__all__: list[str] = [
    "EnumSupervisorVerdict",
    "ModelLocalSupervisorResult",
]
