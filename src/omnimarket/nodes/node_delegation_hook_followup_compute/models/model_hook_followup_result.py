# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Observed follow-up activity, without a correctness judgement."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelHookFollowupSignal(BaseModel):
    """Calls in the receipt-end to next-TERMINAL window of the joined agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    agent_id: str | None
    window_start_ms: int
    window_end_ms: int
    tool_calls: int = Field(ge=0)
    failures: int = Field(ge=0)
    refusals: int = Field(ge=0)
    retries: int = Field(ge=0)
    repeated_digests: tuple[str, ...]
    repeated_work_agents: tuple[str, ...]
    edit_count: int = Field(ge=0)
    write_count: int = Field(ge=0)
    peer_lane_returned_for_fix: bool


class ModelHookReceiptFollowup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt_key: str
    join: Literal["digest", "time", "ambiguous", "none"]
    signal: ModelHookFollowupSignal | None


class ModelHookFollowupResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    followups: tuple[ModelHookReceiptFollowup, ...]
