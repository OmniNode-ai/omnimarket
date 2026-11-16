# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled floor fill alarm check: read the runs, the started lanes with their CLAIMs and the pool, ask the rule,
write one STATUS row per alarm to the ledger.

A row whose alarm key was written within ``repeat_after_minutes`` is not written again, and an alarm whose cause never
clears (a CLAIM that arrived late) is written once, so a lane with a late CLAIM does not alarm on every check.

The row is a ledger STATUS row of a script lane: ``lane=`` one token, ``principal=`` (the settings' authenticated
principal, a header cell the ledger writer accepts only when it is the one it authenticated), ``actor=`` and ``model=none``
as the other script rows carry them.

A source that cannot be read (the runs or the lanes) is an alarm of its own and the check goes on over the others; only
a ledger that cannot be read or written fails the check by name, because no row can be written without it. A run stamped
after the check's clock (skew between the hosts) is read as stamped at the check and counted in the result.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_floor_fill_alarm_compute.handlers.handler_floor_fill_alarm import (
    HandlerFloorFillAlarm,
)
from omnimarket.nodes.node_floor_fill_alarm_compute.models import (
    ModelFloorFillAlarmRequest,
    ModelFloorFillAlarmRow,
    ModelFloorFillPoolRead,
    ModelFloorFillRun,
    ModelFloorFillSentLane,
    ModelFloorFillSourceUnread,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.models import (
    ModelFloorFillAlarmCheckRequest,
    ModelFloorFillAlarmCheckResult,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.protocols.protocol_floor_fill_alarm_check import (
    AlarmSourceError,
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
    stamp = request.now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    cells = (
        stamp,
        "STATUS",
        f"lane={request.row_lane}",
        f"principal={request.row_principal}",
        f"actor={request.row_actor}",
        "model=none",
        f"ticket={request.row_ticket}",
        f"alarm={alarm.kind.value}",
        f"{KEY_CELL}{alarm_key(alarm)}",
        _text(alarm.row_text),
    )
    return CELL_SEPARATOR.join(cells)


def _written_at(row: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(row.split(CELL_SEPARATOR, 1)[0].strip())
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


class _UnboundPortError(AlarmSourceError):
    """A port the handler was built without: the runtime boot builds the handler from its injectable params alone."""


def _clamp_skewed(
    runs: tuple[ModelFloorFillRun, ...], now: datetime
) -> tuple[tuple[ModelFloorFillRun, ...], int]:
    """Runs stamped after ``now`` (the fill's host clock ahead of this one) read as stamped at ``now``, and the count."""
    clamped = 0
    out: list[ModelFloorFillRun] = []
    for run in runs:
        if run.observed_at > now:
            clamped += 1
            run = run.model_copy(update={"observed_at": now})
        out.append(run)
    return tuple(out), clamped


class HandlerFloorFillAlarmCheck:
    """Read, ask the rule, write the rows that are not repeats."""

    def __init__(
        self,
        *,
        runs: ProtocolFloorRunSource | None = None,
        lanes: ProtocolFloorSentLaneSource | None = None,
        pool: ProtocolFloorPoolReader | None = None,
        ledger: ProtocolAlarmLedger | None = None,
        rule: HandlerFloorFillAlarm | None = None,
    ) -> None:
        # The ports default to None so the runtime can construct the handler from its injectable params alone; a handler
        # built that way refuses to check, naming the port, and the binding site (container_live) supplies all four.
        self._runs = runs
        self._lanes = lanes
        self._pool = pool
        self._ledger = ledger
        self._rule = rule or HandlerFloorFillAlarm()

    def handle(
        self, request: ModelFloorFillAlarmCheckRequest
    ) -> ModelFloorFillAlarmCheckResult:
        if self._runs is None:
            raise _UnboundPortError("alarm check port unbound: runs")
        if self._lanes is None:
            raise _UnboundPortError("alarm check port unbound: lanes")
        if self._pool is None:
            raise _UnboundPortError("alarm check port unbound: pool")
        if self._ledger is None:
            raise _UnboundPortError("alarm check port unbound: ledger")
        unread: list[ModelFloorFillSourceUnread] = []
        runs: tuple[ModelFloorFillRun, ...] = ()
        lanes: tuple[ModelFloorFillSentLane, ...] = ()
        try:
            runs = self._runs.read_runs(request.now)
        except AlarmSourceError as exc:
            unread.append(ModelFloorFillSourceUnread(source="runs", cause=str(exc)))
        try:
            lanes = self._lanes.read_sent_lanes()
        except AlarmSourceError as exc:
            unread.append(ModelFloorFillSourceUnread(source="lanes", cause=str(exc)))
        pool: ModelFloorFillPoolRead = self._pool.read_pool()
        runs, clamped = _clamp_skewed(runs, request.now)
        runs_unread = any(item.source == "runs" for item in unread)
        answer = self._rule.handle(
            ModelFloorFillAlarmRequest(
                now=request.now,
                runs=runs,
                sent_lanes=lanes,
                pool=pool,
                unread_sources=tuple(unread),
                slot_threshold=request.slot_threshold,
                idle_runs=request.idle_runs,
                claim_deadline_minutes=request.claim_deadline_minutes,
                # An unread run source is its own alarm; "no run read" would only repeat it under a wrong cause.
                max_run_age_minutes=None
                if runs_unread
                else request.max_run_age_minutes,
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
            runs_clamped=clamped,
            sources_unread=tuple(item.source for item in unread),
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
            if alarm.write_once:
                return True
            written = _written_at(row)
            if written is not None and written >= bound:
                return True
        return False


__all__: list[str] = [
    "HandlerFloorFillAlarmCheck",
    "alarm_key",
    "render_row",
]
