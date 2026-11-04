# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether the session ticks stopped while the landing controller kept running.

THE INCIDENT. After a foreground session restart at 22:20Z on 2026-10-08 the session ticks were
not recreated, and no queue-status run happened for 5 h 39 min. The tick-set check that would
have said so runs only inside a session, and the session was the thing that had stopped.

WHAT IS DECIDED. The queue-status trend file older than the bound (40 minutes) while the landing
controller is live is an alarm, and the fix is to read the launchd queue-status tick. The
merge-throughput heartbeat is no longer read: the launchd tick refreshed it while no
session ran, so the alarm said LIVE for three hours; whether anything landed is the
throughput-results decision. A controller that is not live is the drain watchdog's finding, so it is
skipped here; a controller that could not be read is UNKNOWN, never live.

The PR watcher snapshot is judged the same way against its own eight-minute bar, the landing
reader's DEFAULT_MAX_AGE: on 2026-10-22 its last refresh stayed at 13:49:51Z for 53
minutes while every watcher tick reported ok, and merges fell to 7 in two hours with no alarm.
"""

from __future__ import annotations

from datetime import datetime

from ..models import ModelTickLivenessRequest, ModelTickLivenessResult

LANE = "silent-failure-alarm"
TICKET = "OMN-19998"
FIX = "run onex-host-install queue-status-tick --status, then --kickstart"
WATCHER_FIX = (
    "automatic PR-INVENTORY diagnostics and one scheduled watcher recovery command; "
    "retain the alarm until a complete inventory refresh"
)
FUTURE_SLACK_MINUTES = 5

__all__ = ["HandlerTickLivenessAlarm", "ModelTickLivenessResult"]


def _age(minutes: float) -> str:
    whole = int(minutes)
    return f"{whole // 60}h{whole % 60:02d}m" if whole >= 60 else f"{whole}m"


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class HandlerTickLivenessAlarm:
    """Staleness of the queue-status trend and, when read, the PR watcher snapshot."""

    def handle(self, request: ModelTickLivenessRequest) -> ModelTickLivenessResult:
        """Return LIVE, STALE, SKIPPED or UNKNOWN, with the alarm cells when STALE."""
        if request.controller_live is None:
            return ModelTickLivenessResult(alarm=False, state="UNKNOWN")
        if not request.controller_live:
            return ModelTickLivenessResult(alarm=False, state="SKIPPED")
        stale: list[str] = []
        fixes: list[str] = []
        bars: list[int] = []
        oldest = 0.0
        sources = [
            ("queue-status trend file", request.trend_at, request.max_age_minutes, FIX)
        ]
        if request.watcher_read:
            sources.append(
                (
                    "PR watcher snapshot",
                    request.watcher_at,
                    request.watcher_max_age_minutes,
                    WATCHER_FIX,
                )
            )
        for name, at, max_age, fix in sources:
            before = len(stale)
            minutes = 0.0 if at is None else (request.now - at).total_seconds() / 60
            if at is None:
                stale.append(f"{name}: absent")
            elif minutes < (
                0 if name == "PR watcher snapshot" else -FUTURE_SLACK_MINUTES
            ):
                stale.append(f"{name}: stamp {_stamp(at)} is in the future")
            elif minutes > max_age:
                stale.append(f"{name}: {_age(minutes)} old (last {_stamp(at)})")
                oldest = max(oldest, minutes)
            if len(stale) > before:
                fixes.append(fix)
                bars.append(max_age)
        if not stale:
            return ModelTickLivenessResult(alarm=False, state="LIVE")
        finding = "; ".join(stale)
        fix_text = "; ".join(fixes)
        cells = (
            f"FRICTION | lane={LANE} | ticket={TICKET} | actor=launchd:{LANE} | "
            f"class=tick-liveness | stale={len(stale)} | "
            f"cost=est ~{max(int(oldest), *bars)} minutes without a "
            f"fresh tick source while the landing controller is live | {finding}; fix: {fix_text}"
        )
        return ModelTickLivenessResult(
            alarm=True,
            state="STALE",
            stale=tuple(stale),
            fix=fix_text,
            friction_cells=cells,
            alert_text=f"TICK LIVENESS {finding}; landing controller live; fix: {fix_text}",
        )
