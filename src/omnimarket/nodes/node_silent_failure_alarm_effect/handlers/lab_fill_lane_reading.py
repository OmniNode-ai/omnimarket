# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a lab-fill run's lane table into the facts the placement alarm judges (OMN-23025).

On 2026-11-08 the alarm read two runs as ``placed 9 of 33`` and named no reason. The fill writes its
STATUS row while its detached send jobs are still building: each lane it handed over reads
``pending`` there, and every one of the 24 got its CLAIM row one to ten minutes later. The row's
``placed`` is therefore the count at the moment of the read, not the count the run reached. The
lanes the fill skipped named their reason in that same row (``skipped:<reason>``), and the runner's
lane-end report, a STATUS row since OMN-21822, names the status a lane ended on; the alarm read
neither, so it could only say ``no-refusal-reported``.

This reader gives each lane of the row its own outcome from the ledger's own rows: a pending lane
with a CLAIM row is placed, a pending lane inside the send cap is still in flight and is judged
neither way yet, and a pending lane past the cap with no CLAIM row is a refusal named
``send-job-no-claim``. A skipped lane is a refusal named by the fill's own reason.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from omnimarket.models.silent_failure_alarm import ModelLaneRefusal, ModelPlacementRun

NO_CLAIM = "send-job-no-claim"
_UNSLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class LabFillLane:
    """One entry of a fill row's ``lanes=`` cell: ``<lane>@<host>:<state>[:<reason>]``."""

    name: str
    host: str
    state: str
    reason: str


@dataclass(frozen=True, slots=True)
class LabFillRunRow:
    """One lab-fill STATUS row: its counts at the moment it was written and its lane table."""

    run_key: str
    stamp: datetime
    dispatched: int
    placed: int
    lanes: tuple[LabFillLane, ...]


def status_slug(text: str) -> str:
    """A reason as the alarm's status token: lowercase letters, digits and hyphens."""
    return _UNSLUG.sub("-", text.lower()).strip("-") or "unknown"


def parse_lanes(cell: str | None) -> tuple[LabFillLane, ...]:
    """The lanes of a ``lanes=`` cell; an entry that is not ``<lane>@<host>:<state>`` is dropped."""
    lanes: list[LabFillLane] = []
    for entry in (cell or "").split(","):
        name, at, rest = entry.strip().partition("@")
        host, colon, state = rest.partition(":")
        if not (name and at and host and colon and state):
            continue
        word, _, reason = state.partition(":")
        lanes.append(LabFillLane(name, host, word, reason))
    return tuple(lanes)


def settle_run(
    row: LabFillRunRow,
    claimed: Mapping[str, datetime],
    now: datetime,
    send_cap: timedelta,
) -> tuple[ModelPlacementRun, tuple[ModelLaneRefusal, ...]]:
    """The run as the ledger reads it at ``now``, and the refusal each unplaced lane carries.

    A row with no lane table keeps the counts it states. Otherwise ``placed`` gains each pending
    lane that has a CLAIM row, and ``dispatched`` loses each pending lane still inside the send cap."""
    placed, dispatched = row.placed, row.dispatched
    refusals: list[ModelLaneRefusal] = []
    for lane in row.lanes:
        if lane.state == "placed":
            continue
        if lane.state == "pending":
            at = claimed.get(lane.name)
            if at is not None and at <= now:
                placed += 1
            elif now - row.stamp < send_cap:
                dispatched -= 1
            else:
                refusals.append(
                    ModelLaneRefusal(
                        run_key=row.run_key, lane=lane.name, status=NO_CLAIM
                    )
                )
            continue
        reason = lane.reason or lane.state
        refusals.append(
            ModelLaneRefusal(
                run_key=row.run_key, lane=lane.name, status=status_slug(reason)
            )
        )
    dispatched = max(dispatched, 0)
    return (
        ModelPlacementRun(
            run_key=row.run_key,
            stamp=row.stamp,
            dispatched=dispatched,
            placed=min(placed, dispatched),
        ),
        tuple(refusals),
    )
