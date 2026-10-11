# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verify a lab-fill tick's fan-out from bus dispatch records (OMN-20867).

The fan-out a tick reports is derived only from dispatch records read back from
the bus for that tick; a claimed lane with no record is a failure, never a success.
"""

from __future__ import annotations

from ..models import (
    EnumLabFillPlanFailure,
    ModelLabFillDispatchRecord,
    ModelLabFillFanoutRequest,
    ModelLabFillFanoutResult,
)


class HandlerLabFillFanoutReadback:
    """Count detached dispatch records and fail for unconfirmed claimed lanes."""

    def handle(self, request: ModelLabFillFanoutRequest) -> ModelLabFillFanoutResult:
        confirmed: dict[str, ModelLabFillDispatchRecord] = {}
        other_tick_records = 0
        for record in request.records:
            if record.tick_id != request.tick_id:
                other_tick_records += 1
            elif record.detached:
                confirmed.setdefault(record.lane, record)
        unconfirmed = tuple(
            lane
            for lane in dict.fromkeys(request.claimed_lanes)
            if lane not in confirmed
        )
        return ModelLabFillFanoutResult(
            tick_id=request.tick_id,
            fanned_out=len(confirmed),
            confirmed=tuple(confirmed.values()),
            unconfirmed=unconfirmed,
            other_tick_records=other_tick_records,
            terminal_failure_cause=(
                EnumLabFillPlanFailure.FANOUT_UNPROVEN if unconfirmed else None
            ),
        )
