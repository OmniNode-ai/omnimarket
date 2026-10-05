# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The state_io row of one PR in the handoff workflow (OMN-20636)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.events.pr_state import ModelPrStateEmitRequest
from omnimarket.models.pr_handoff import (
    EnumPrHandoffState,
    ModelPrHandoffDecision,
    ModelPrHandoffRequested,
)


class ModelPrHandoffEpisode(BaseModel):
    """One request's run through the FSM; the row keeps the newest one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelPrHandoffRequested
    state: EnumPrHandoffState
    deadline_at: Annotated[datetime, AwareDatetime] | None = None
    last_decision: ModelPrHandoffDecision | None = None
    ledger_request_id: UUID | None = None
    rows: str | None = None
    append_attempts: int = Field(default=0, ge=0)
    append_abandoned: bool = Field(
        default=False,
        description="The runtime gave the append in flight up; the next request ends it.",
    )


class ModelPrHandoffWorkflowRow(BaseModel):
    """Keyed ``handoff_key`` (repo#n): the newest observation and the newest episode.

    The observation is folded whether or not a request is live, so a request
    for a PR the watcher already saw is decided in its own leg; an older
    observation never replaces a newer one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    handoff_key: str = Field(..., min_length=3)
    observation: ModelPrStateEmitRequest | None = None
    episode: ModelPrHandoffEpisode | None = None


__all__: list[str] = ["ModelPrHandoffEpisode", "ModelPrHandoffWorkflowRow"]
