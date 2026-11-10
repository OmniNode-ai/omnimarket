# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The recent-delegations envelope, in the v1 shape the post-login panel reads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelDelegationRun(BaseModel):
    """One delegation run with its measured cost and savings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    task_type: str
    model: str
    status: Literal["passed", "failed"]
    cost_usd: float
    saved_usd: float
    occurred_at: str


class ModelRecentDelegations(BaseModel):
    """A bounded list of the newest runs of one tenant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: str
    count: int
    data_state: Literal["ok", "insufficient_data"]
    runs: list[ModelDelegationRun]
