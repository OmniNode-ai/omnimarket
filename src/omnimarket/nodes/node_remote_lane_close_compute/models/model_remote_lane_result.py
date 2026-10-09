# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the remote-lane closing decision (OMN-20669)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

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
