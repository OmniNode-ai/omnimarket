# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two alarm rules of workflow 2, and the ledger rows they write.

* ``floor-fill-no-placement``: free slots above the bar and the fill placed 0 lanes, for N scheduled runs in a row.
  The streak is carried from run to run in the workflow result (Temporal's last completion result).
* ``floor-fill-no-claim``: a lane the fill placed wrote no CLAIM inside the claim window.

No alarm row is written when neither holds. The rows are STATUS rows with ``src=alarm`` (the alarm row shape the
prove-running workflow writes), so the ledger's typed grammar accepts them as they are.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_floor_fill_temporal_workflow.models.model_floor_fill_temporal import (
    ModelFloorFillAlarmDecision,
    ModelFloorFillAlarmRequest,
)

ALARM_NO_PLACEMENT = "floor-fill-no-placement"
ALARM_NO_CLAIM = "floor-fill-no-claim"
ACTOR = "temporal:floor-fill-schedule"
_CELL_MAX = 240


def _cell(text: str) -> str:
    """One ledger cell: no pipe, no line break, bounded; an empty value reads ``none``."""
    flat = re.sub(r"\s+", " ", text.replace("|", "/")).strip()
    return flat[:_CELL_MAX] or "none"


def _token(text: str) -> str:
    """A key=value value with no spaces."""
    return re.sub(r"\s+", "-", _cell(text))


class HandlerFloorFillAlarmRule:
    """Decide the streak and the alarms of one run, and build each alarm row."""

    def handle(
        self, request: ModelFloorFillAlarmRequest
    ) -> ModelFloorFillAlarmDecision:
        rule = request.rule
        free = request.pool.free_slots
        placed = request.fill.placed_lanes
        zero_run = not placed and free > rule.free_slots_above
        streak = request.prior_zero_streak + 1 if zero_run else 0
        head = (
            f"{request.written_at} | STATUS | lane={request.lane} | ticket={request.ticket}"
            f" | actor={ACTOR} | model=none | src=alarm"
        )
        tail = (
            f"workflow={_token(request.workflow_id)} | step={_token(request.step_key)}"
        )
        alarms: list[str] = []
        rows: list[str] = []
        if streak >= rule.zero_runs:
            alarms.append(ALARM_NO_PLACEMENT)
            rows.append(
                f"{head} | alarm={ALARM_NO_PLACEMENT} | free_slots={free}"
                f" | unowned_work={request.pool.unowned_work} | placed=0 | zero_streak={streak}"
                f" | why_none={_cell(request.fill.why_none)} | read_at={_token(request.pool.read_at)} | {tail}"
                f" | free slots stayed above {rule.free_slots_above} and the floor fill placed 0 lanes"
                f" for {streak} scheduled runs in a row"
            )
        unclaimed = request.claims.unclaimed if request.claims is not None else ()
        if unclaimed:
            alarms.append(ALARM_NO_CLAIM)
            read_at = request.claims.read_at if request.claims is not None else "none"
            rows.append(
                f"{head} | alarm={ALARM_NO_CLAIM} | unclaimed={_token(','.join(unclaimed))}"
                f" | placed={len(placed)} | placed_at={_token(request.fill.read_at)} | read_at={_token(read_at)}"
                f" | {tail} | {len(unclaimed)} placed lane(s) wrote no CLAIM within"
                f" {rule.claim_window_seconds // 60} minutes of placement"
            )
        return ModelFloorFillAlarmDecision(
            zero_streak=streak, alarms=tuple(alarms), rows=tuple(rows)
        )
