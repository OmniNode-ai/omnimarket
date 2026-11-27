# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One row of the lane work workflow table (OMN-26708)."""

from __future__ import annotations

from typing import Any

from omnibase_core.enums.enum_lane_work_state import EnumLaneWorkState
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelLaneWorkRow(BaseModel):
    """The fold of one workflow's receipts: current state and, once it ends, its terminal.

    Every column is derived from receipt fields. `duration_seconds` is the span
    from the first receipt seen to the latest one, `help_request_count` counts the
    receipts that entered `help_requested`, and `transitions_taken` counts the
    `transition` receipts. None of them has a default: a live workflow's duration
    runs to its latest receipt, and the terminal columns are null only while the
    workflow is live.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str
    state: EnumLaneWorkState
    last_event_kind: str
    seq: int = Field(ge=1)
    started_at: AwareDatetime
    last_receipt_at: AwareDatetime
    transitions_taken: int = Field(ge=0)
    help_request_count: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    terminal_kind: str | None = Field(
        default=None, description="handed_off or blocked once the workflow ended."
    )
    ended_at: AwareDatetime | None = None

    def to_exposure_row(self, *, now: AwareDatetime) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "state": self.state.value,
            "last_event_kind": self.last_event_kind,
            "seq": self.seq,
            "started_at": self.started_at.isoformat(),
            "last_receipt_at": self.last_receipt_at.isoformat(),
            "transitions_taken": self.transitions_taken,
            "help_request_count": self.help_request_count,
            "duration_seconds": self.duration_seconds,
            "terminal_kind": self.terminal_kind,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "projected_at": now.isoformat(),
        }
