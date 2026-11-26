# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The receipt, the state it leaves and every output it lists: the commit point (OMN-26721)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from omnibase_core.models.lane_work import ModelLaneWorkAction, ModelLaneWorkReceipt
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_output import (
    ModelLaneWorkOutput,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reducer_state import (
    ModelLaneWorkReducerState,
)


class ModelLaneWorkTransitionRecord(BaseModel):
    """Published before any output of its receipt. The orchestrator rebuilds a workflow
    from the newest record of each workflow, so no second store is needed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt: ModelLaneWorkReceipt
    correlation_id: UUID
    state: ModelLaneWorkReducerState
    outputs: tuple[ModelLaneWorkOutput, ...] = ()
    attempt: int = Field(default=1, ge=1)
    last_head_change_at: datetime | None = None
    pending_action: ModelLaneWorkAction | None = None
    proof_receipt_id: str | None = None
    proof_summary: str | None = None
    claim_prs: tuple[str, ...] = ()
    claim_tickets: tuple[str, ...] = ()
    body_ticket_ids: tuple[str, ...] = ()
    started_at: datetime
    transitions_taken: int = Field(default=0, ge=0)
    help_requests_by_tier: dict[str, int] = Field(default_factory=dict)
    help_action_ids: tuple[str, ...] = ()


__all__: list[str] = ["ModelLaneWorkTransitionRecord"]
