# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled floor fill alarm check: read the runs, the started lanes with their CLAIMs and the pool, ask the rule,
write one STATUS row per alarm to the ledger.

A source that cannot be read fails the check by name and writes no row: a check that read nothing never reports quiet.
A row whose alarm key was written within ``repeat_after_minutes`` is not written again, so a lane with a late CLAIM
does not alarm on every check.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from omnimarket.nodes.node_floor_fill_alarm_compute.handlers.handler_floor_fill_alarm import (
    HandlerFloorFillAlarm,
)
from omnimarket.nodes.node_floor_fill_alarm_compute.models import (
    ModelFloorFillAlarmRequest,
    ModelFloorFillAlarmRow,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.models import (
    ModelFloorFillAlarmCheckRequest,
    ModelFloorFillAlarmCheckResult,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.protocols.protocol_floor_fill_alarm_check import (
    ProtocolAlarmLedger,
    ProtocolFloorPoolReader,
    ProtocolFloorRunSource,
    ProtocolFloorSentLaneSource,
)

CELL_SEPARATOR = " | "
KEY_CELL = "alarm-key="


def alarm_key(row: ModelFloorFillAlarmRow) -> str:
    """A short stable digest of the alarm's dedupe key: the cell the ledger carries to find a repeat."""
    return hashlib.sha256(row.dedupe_key.encode("utf-8")).hexdigest()[:12]


def _text(value: str) -> str:
    """A ledger cell holds no separator and no line break."""
    return " ".join(value.replace(CELL_SEPARATOR, "; ").split())


def render_row(
    request: ModelFloorFillAlarmCheckRequest, alarm: ModelFloorFillAlarmRow
) -> str:
    stamp = request.now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cells = (
        stamp,
        "STATUS",
        f"lane={request.row_lane}",
        f"ticket={request.row_ticket}",
        f"alarm={alarm.kind.value}",
        f"{KEY_CELL}{alarm_key(alarm)}",
        _text(alarm.row_text),
    )
    return CELL_SEPARATOR.join(cells)


def _written_at(row: str) -> datetime | None:
    try:
        return datetime.fromisoformat(row.split(CELL_SEPARATOR, 1)[0].strip())
    except ValueError:
        return None


class HandlerFloorFillAlarmCheck:
    """Read, ask the rule, write the rows that are not repeats."""

    def __init__(
        self,
        *,
        runs: ProtocolFloorRunSource,
        lanes: ProtocolFloorSentLaneSource,
        pool: ProtocolFloorPoolReader,
        ledger: ProtocolAlarmLedger,
        rule: HandlerFloorFillAlarm | None = None,
    ) -> None:
        self._runs = runs
        self._lanes = lanes
        self._pool = pool
        self._ledger = ledger
        self._rule = rule or HandlerFloorFillAlarm()

    def handle(
        self, request: ModelFloorFillAlarmCheckRequest
    ) -> ModelFloorFillAlarmCheckResult:
        runs = self._runs.read_runs(request.now)
        lanes = self._lanes.read_sent_lanes()
        pool = self._pool.read_pool()
        answer = self._rule.handle(
            ModelFloorFillAlarmRequest(
                now=request.now,
                runs=runs,
                sent_lanes=lanes,
                pool=pool,
                slot_threshold=request.slot_threshold,
                idle_runs=request.idle_runs,
                claim_deadline_minutes=request.claim_deadline_minutes,
                max_run_age_minutes=request.max_run_age_minutes,
            )
        )
        existing = self._ledger.rows()
        written: list[str] = []
        suppressed = 0
        for alarm in answer.alarms:
            if self._repeat(existing, alarm, request):
                suppressed += 1
                continue
            row = render_row(request, alarm)
            self._ledger.append(row)
            written.append(row)
        return ModelFloorFillAlarmCheckResult(
            alarms_found=len(answer.alarms),
            rows_written=tuple(written),
            rows_suppressed=suppressed,
            runs_read=answer.runs_read,
            lanes_read=answer.lanes_read,
            pool_free_slots=pool.free_slots,
        )

    @staticmethod
    def _repeat(
        existing: tuple[str, ...],
        alarm: ModelFloorFillAlarmRow,
        request: ModelFloorFillAlarmCheckRequest,
    ) -> bool:
        cell = f"{KEY_CELL}{alarm_key(alarm)}"
        bound = request.now - timedelta(minutes=request.repeat_after_minutes)
        for row in existing:
            if cell not in (c.strip() for c in row.split(CELL_SEPARATOR)):
                continue
            written = _written_at(row)
            if written is not None and written >= bound:
                return True
        return False


__all__: list[str] = [
    "HandlerFloorFillAlarmCheck",
    "alarm_key",
    "render_row",
]
