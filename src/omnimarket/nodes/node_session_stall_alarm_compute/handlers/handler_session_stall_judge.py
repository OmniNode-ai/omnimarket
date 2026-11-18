# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether the orchestrating session stopped dequeuing input or firing its ticks (OMN-25405).

THE INCIDENT. On 2026-11-17 the orchestrating session's transcript held queued input with no dequeue from 07:58:57Z to
11:37:15Z, and no scheduled task fired between 07:47:56Z and 11:37:15Z against a cadence of about 30 minutes. Nothing
alarmed, because every check that could have said so ran inside the session that had stopped.

WHAT IS DECIDED, from the rows a reader supplies and ``now`` alone (no clock, no file):

* the oldest enqueue with no dequeue (or remove) after it has waited ``dequeue_after_seconds`` (15 minutes), or
* a recurring session tick is declared and no scheduled task fired for ``task_after_seconds`` (45 minutes).

PAIRING. The session's queue is first in, first out, so each dequeue or remove ends the oldest enqueue still waiting;
a dequeue with nothing waiting (its enqueue is before the rows the reader saw) ends nothing. Rows are ordered by time,
and at one instant an enqueue is taken before a task firing before a dequeue, so equal times pair as they were written.
The scheduled-task clock starts at the last task row; with no task row it starts at the first row, because a stall
can be named only for a quiet that the rows cover; with no rows at all there is nothing to judge.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

from omnimarket.models.session_stall import (
    EnumQueueOperation,
    EnumSessionStallReason,
    ModelQueueRow,
    ModelSessionStallRequest,
    ModelSessionStallVerdict,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_ORDER = {
    EnumQueueOperation.ENQUEUE: 0,
    EnumQueueOperation.TASK_FIRED: 1,
    EnumQueueOperation.DEQUEUE: 2,
    EnumQueueOperation.REMOVE: 2,
}


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_STAMP)


def _age(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600}h{whole % 3600 // 60:02d}m{whole % 60:02d}s"


def _ordered(request: ModelSessionStallRequest) -> list[ModelQueueRow]:
    seen = [row for row in request.rows if row.at <= request.now]
    return sorted(seen, key=lambda row: (row.at, _ORDER[row.operation]))


class HandlerSessionStallJudge:
    def handle(self, request: ModelSessionStallRequest) -> ModelSessionStallVerdict:
        rows = _ordered(request)
        waiting: deque[datetime] = deque()
        last_task: datetime | None = None
        for row in rows:
            if row.operation is EnumQueueOperation.ENQUEUE:
                waiting.append(row.at)
            elif row.operation is EnumQueueOperation.TASK_FIRED:
                last_task = row.at
            elif waiting:
                waiting.popleft()
        reasons: list[EnumSessionStallReason] = []
        cells: list[str] = []
        oldest = waiting[0] if waiting else None
        if oldest is not None:
            waited = (request.now - oldest).total_seconds()
            if waited >= request.dequeue_after_seconds:
                reasons.append(EnumSessionStallReason.OLDEST_ENQUEUE_UNDEQUEUED)
                cells.append(
                    f"the oldest of {len(waiting)} queued input(s) was enqueued {_stamp(oldest)} "
                    f"and has had no dequeue for {_age(waited)} "
                    f"(bar {_age(request.dequeue_after_seconds)})"
                )
        if request.tick_declared and rows:
            since = last_task if last_task is not None else rows[0].at
            quiet = (request.now - since).total_seconds()
            if quiet >= request.task_after_seconds:
                reasons.append(EnumSessionStallReason.NO_SCHEDULED_TASK)
                which = (
                    f"since the last at {_stamp(last_task)}"
                    if last_task is not None
                    else f"in the rows read, which begin {_stamp(since)}"
                )
                cells.append(
                    f"no scheduled task fired for {_age(quiet)} {which} "
                    f"(bar {_age(request.task_after_seconds)}) while a recurring tick is declared"
                )
        stalled = bool(reasons)
        return ModelSessionStallVerdict(
            stalled=stalled,
            reasons=tuple(reasons),
            queued=len(waiting),
            oldest_enqueue_at=oldest,
            last_task_at=last_task,
            detail=(
                "the orchestrating session stopped taking its input or firing its ticks: "
                + "; ".join(cells)
                if stalled
                else ""
            ),
            evidence=(
                f"judged {_stamp(request.now)} over {len(rows)} row(s); "
                + "; ".join(cells)
                if stalled
                else ""
            ),
        )


__all__ = ["HandlerSessionStallJudge"]
