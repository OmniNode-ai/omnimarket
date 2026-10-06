# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of node_pr_handoff_decision_compute (OMN-20636)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.events.pr_state import ModelPrStateEmitRequest
from omnimarket.models.pr_handoff.model_pr_handoff_requested import (
    ModelPrHandoffRequested,
)


class ModelPrHandoffDecisionRequest(BaseModel):
    """One request, the newest live observation of its PR, live holds, and the time.

    ``observation`` is the PR watcher's newest ``pr.state.observed`` for the PR
    (None when the watcher has not seen it). ``ledger_holds`` are the ids of the
    live ledger HOLD rows that name the PR or the requesting lane. ``now`` is
    the evaluating message's time: the compute reads no clock.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelPrHandoffRequested
    observation: ModelPrStateEmitRequest | None = None
    ledger_holds: tuple[str, ...] = Field(default=())
    now: Annotated[datetime, AwareDatetime]


__all__: list[str] = ["ModelPrHandoffDecisionRequest"]
