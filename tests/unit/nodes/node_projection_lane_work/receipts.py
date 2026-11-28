# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipt builders shared by the lane work projection tests (OMN-26708)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from omnibase_core.enums.enum_lane_work_event import EnumLaneWorkEvent
from omnibase_core.enums.enum_lane_work_state import EnumLaneWorkState
from omnibase_core.models.lane_work import ModelLaneWorkReceipt

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)

S = EnumLaneWorkState
E = EnumLaneWorkEvent


def receipt(
    workflow_id: str,
    seq: int,
    event: EnumLaneWorkEvent,
    from_state: EnumLaneWorkState,
    to_state: EnumLaneWorkState,
    *,
    minutes: int,
    kind: Literal["transition", "refused", "duplicate"] = "transition",
    refusal: str | None = None,
) -> ModelLaneWorkReceipt:
    return ModelLaneWorkReceipt(
        workflow_id=workflow_id,
        seq=seq,
        receipt_id=f"{workflow_id}-r{seq}",
        kind=kind,
        input_id=f"{workflow_id}-i{seq}",
        event_kind=event,
        from_state=from_state,
        to_state=to_state,
        action_ids=(),
        refusal=refusal,
        occurred_at=T0 + timedelta(minutes=minutes),
    )


def live_workflow(workflow_id: str) -> list[ModelLaneWorkReceipt]:
    """start, worker started, claim confirmed: three receipts, still live."""
    return [
        receipt(workflow_id, 1, E.START, S.QUEUED, S.AWAITING_WORKER, minutes=0),
        receipt(
            workflow_id,
            2,
            E.WORKER_STARTED,
            S.AWAITING_WORKER,
            S.CONFIRMING_CLAIM,
            minutes=2,
        ),
        receipt(
            workflow_id, 3, E.CLAIM_CONFIRMED, S.CONFIRMING_CLAIM, S.CLAIMED, minutes=3
        ),
    ]


def handed_off_workflow(workflow_id: str) -> list[ModelLaneWorkReceipt]:
    """A workflow that asks for help once, then hands off."""
    return [
        *live_workflow(workflow_id),
        receipt(workflow_id, 4, E.CHECK_RED, S.CLAIMED, S.RED, minutes=5),
        receipt(workflow_id, 5, E.CAUSE_UNKNOWN, S.RED, S.HELP_REQUESTED, minutes=6),
        receipt(
            workflow_id, 6, E.HELP_CAUSE_FOUND, S.HELP_REQUESTED, S.FIXING, minutes=9
        ),
        receipt(workflow_id, 7, E.PROOF_PASSED, S.FIXING, S.PROVEN, minutes=20),
        receipt(workflow_id, 8, E.HANDOFF_WRITTEN, S.PROVEN, S.HANDED_OFF, minutes=25),
    ]


def blocked_workflow(workflow_id: str) -> list[ModelLaneWorkReceipt]:
    """A workflow whose worker never started."""
    return [
        receipt(workflow_id, 1, E.START, S.QUEUED, S.AWAITING_WORKER, minutes=0),
        receipt(
            workflow_id, 2, E.DEADLINE_PASSED, S.AWAITING_WORKER, S.BLOCKED, minutes=10
        ),
    ]
