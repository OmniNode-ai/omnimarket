# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sum the lab-idle FRICTION cost since a start for the status line, without I/O (OMN-22885).

The lab-fill tick writes one FRICTION row of class lab-idle each time it leaves slots free, with
`cost=est <n> lane-minutes (...)`. On 2026-11-07, 30 such rows from 00:35Z to 10:36Z carried 7,100
lane-minutes and nothing summed them. This handler sums them from a start and renders the
segment the status line prints. A lab-idle row whose cost cannot be read is counted as
unreadable, never as zero.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.nodes.node_lab_idle_cost_compute.models import (
    ModelLabIdleCostRequest,
    ModelLabIdleCostResult,
)

STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
COST = re.compile(r"^est (?P<amount>\d+(?:\.\d+)?) lane-(?P<unit>minutes|hours)\b")
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ORCHESTRATOR_LANE = re.compile(r"^orchestrator(-[\w.]+)?$")


def last_orchestrator_start(rows: tuple[str, ...]) -> str | None:
    """Stamp of the newest CLAIM row of an orchestrator lane: the start the sum counts from."""
    best: str | None = None
    for line in rows:
        row = parse_row(line, types=frozenset({"CLAIM"}))
        if row is None or not ORCHESTRATOR_LANE.match(row.token("lane") or ""):
            continue
        if best is None or row.stamp > best:
            best = row.stamp
    return best


def sum_start(rows: tuple[str, ...]) -> str | None:
    """The start the status line sums from: the last orchestrator CLAIM, else the first row of the
    ledger it was given (the segment names the stamp, so a reader sees which start it used)."""
    claimed = last_orchestrator_start(rows)
    if claimed is not None:
        return claimed
    return next(
        (row.stamp for line in rows if (row := parse_row(line)) is not None), None
    )


def _calendar_moment(stamp: str) -> datetime | None:
    """The stamp as a UTC moment, or None when it is not a real date and time."""
    if not STAMP.match(stamp):
        return None
    try:
        return datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _lane_minutes(cost: str | None) -> float | None:
    match = COST.match(cost or "")
    if match is None:
        return None
    amount = float(match.group("amount"))
    return amount * 60 if match.group("unit") == "hours" else amount


def _figure(value: float) -> str:
    return f"{value:,.0f}" if value == int(value) else f"{value:,.1f}"


class HandlerLabIdleCost:
    """Pure: ledger rows and a start in, the lane-minutes of lab-idle FRICTION since then out."""

    def handle(self, request: ModelLabIdleCostRequest) -> ModelLabIdleCostResult:
        start = request.since or sum_start(request.rows)
        if start is None:
            raise ValueError("since is empty and rows hold no ledger row to count from")
        since = _calendar_moment(start)
        if since is None:
            raise ValueError(
                f"since must be a UTC stamp like 2026-11-07T00:35:28Z, got {start!r}"
            )
        if request.until:
            until = _calendar_moment(request.until)
            if until is None:
                raise ValueError(
                    f"until must be a UTC stamp like 2026-11-07T10:36:26Z, got {request.until!r}"
                )
            if until < since:
                raise ValueError(
                    f"until must not be earlier than since, got since={start!r} "
                    f"until={request.until!r}"
                )
        total = 0.0
        counted = unreadable = 0
        for line in dict.fromkeys(
            line.strip() for line in request.rows if line.strip()
        ):
            row = parse_row(line, types=frozenset({"FRICTION"}))
            if row is None or row.value("class") != "lab-idle":
                continue
            if row.stamp < start or (request.until and row.stamp > request.until):
                continue
            minutes = _lane_minutes(row.value("cost"))
            if minutes is None:
                unreadable += 1
                continue
            total += minutes
            counted += 1
        segment = f"lab-idle {_figure(total)} lane-min ({counted} rows since {start}"
        segment += f", {unreadable} unreadable)" if unreadable else ")"
        return ModelLabIdleCostResult(
            lane_minutes=total,
            rows=counted,
            unreadable=unreadable,
            since=start,
            segment=segment,
        )
