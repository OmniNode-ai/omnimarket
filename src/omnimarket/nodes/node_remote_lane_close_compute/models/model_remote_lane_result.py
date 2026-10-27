# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the remote-lane closing decision (OMN-20669)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .model_remote_lane_receipt_usage import ModelRemoteLaneReceiptUsage

_FROZEN = ConfigDict(frozen=True, extra="forbid")

LANE_OUTCOMES = frozenset(
    {"blocked", "partial", "handed-off", "done", "skipped-owned", "unknown"}
)
REJECTED_OUTCOME = "rejected-no-delegation"
FAILED_OUTCOME = "failed"


class ModelRemoteLaneResultRequest(BaseModel):
    """The engine's exit code and the lane's final message."""

    model_config = _FROZEN

    engine_exit_code: int
    final_message: str | None = None
    engine: str = "UNKNOWN"
    engine_output: tuple[dict[str, Any], ...] | None = Field(
        default=None,
        description="Ordered engine JSON events for this attempt, including tool results. None means not supplied.",
    )
    brief: str = Field(
        default="", description="Brief carrying optional Contract: and Attempt: lines."
    )
    wall_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    delegation_reasons: tuple[str, ...] = Field(
        default=(
            "no-text-or-code",
            "route-refused:<status>",
            "route-unavailable:<run id>",
            "read-only-lane",
        ),
        description="The ledger's declared delegation_reason set, named in a refusal.",
    )


class ModelRemoteLaneResult(BaseModel):
    """How the lane closes: its outcome, the delegation problem, the delegation cells."""

    model_config = _FROZEN

    outcome: str
    problem: str | None
    declared_outcome: str
    delegation: dict[str, str]
    waiting_on_background: bool
    usage: ModelRemoteLaneReceiptUsage
    ledger_cells: dict[str, str] = Field(default_factory=dict)
