# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether a pool host ran lanes and sent no hook event (OMN-18603).

THE INCIDENT. h202's hook-emit drainer stopped publishing at 2026-10-09T15:45:48Z. Remote lanes
kept running on the host, no hook event of theirs reached the projection for four and a half days,
and nothing said so: the capture is the only record of what a lane did, and the thing that watches
it ran on the same host.

WHAT IS DECIDED. A pool host is QUIET when a ledger row put a lane on it inside the last hour, it
has sent no hook event for ``quiet_minutes`` (30), and lane rows have kept arriving for the host for
at least that long since its last event. The last condition is what keeps an idle host quiet by
design: the closing row of its last lane lands seconds after its final hook event, so a host that
simply stopped working after its last lane never reads as quiet, while a host whose lanes started
after its last event and have run half an hour without one does.

A hook-event table that could not be read is UNKNOWN, never LIVE: the alarm that checks capture
must not pass because the capture's own store was unreachable.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from ..models import (
    ModelHostActivity,
    ModelQuietHostRequest,
    ModelQuietHostResult,
)

LANE = "silent-failure-alarm"
TICKET = "OMN-18603"
FIX = (
    "on each named host run bash omniclaude/scripts/install-hook-emit-drainer.sh --status "
    "and, when the drainer is not active, bash omniclaude/scripts/install-hook-emit-drainer.sh"
)
# The ledger row for the last lane lands seconds after that lane's final hook event; rows inside
# this slack of the last event belong to the work the event already recorded.
EVENT_SLACK = timedelta(minutes=5)
FUTURE_SLACK = timedelta(minutes=5)

__all__ = ["HandlerQuietHostAlarm", "ModelQuietHostResult", "host_id"]


def host_id(hostname: str, aliases: Mapping[str, str]) -> str | None:
    """The pool host id for a hook event's ``host`` value, or None for a host nobody declared.

    ``aliases`` maps a host id to the lowercased first DNS label its hook events carry. The map is
    deployment data: the caller reads it from its overlay and passes it in.
    """
    by_alias = {alias: host for host, alias in aliases.items()}
    return by_alias.get(hostname.strip().lower().split(".", 1)[0])


def _age(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02d}h"
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class HandlerQuietHostAlarm:
    """Which pool hosts ran lanes with no hook event to show for it."""

    def handle(self, request: ModelQuietHostRequest) -> ModelQuietHostResult:
        """Return LIVE, QUIET or UNKNOWN, with the alarm cells when QUIET."""
        if not request.events_readable:
            return ModelQuietHostResult(alarm=False, state="UNKNOWN")
        quiet: list[str] = []
        findings: list[str] = []
        longest = timedelta(0)
        for host in sorted(request.hosts, key=lambda h: h.host):
            since = self._quiet_since(host, request)
            if since is None:
                continue
            quiet.append(host.host)
            longest = max(longest, request.now - since)
            last = (
                "no hook event in the read window"
                if host.last_event_at is None
                else f"last hook event {_stamp(host.last_event_at)}"
                f" ({_age(request.now - host.last_event_at)} ago)"
            )
            findings.append(
                f"{host.host}: {last}; lanes ran on it for {_age(request.now - since)}"
            )
        if not quiet:
            return ModelQuietHostResult(alarm=False, state="LIVE")
        finding = "; ".join(findings)
        names = ",".join(quiet)
        minutes = max(int(longest.total_seconds() // 60), request.quiet_minutes)
        cells = (
            f"FRICTION | lane={LANE} | ticket={TICKET} | actor=launchd:{LANE} | "
            f"class=quiet-host | hosts={names} | "
            f"cost=est ~{minutes} minutes of lane work with no captured hook events | "
            f"{finding}; fix: {FIX}"
        )
        return ModelQuietHostResult(
            alarm=True,
            state="QUIET",
            quiet_hosts=tuple(quiet),
            findings=tuple(findings),
            fix=FIX,
            friction_cells=cells,
            alert_text=f"QUIET HOST {finding}; fix: {FIX}",
        )

    @staticmethod
    def _quiet_since(
        host: ModelHostActivity, request: ModelQuietHostRequest
    ) -> datetime | None:
        """When lanes first ran on the host after its last event, if that makes it quiet now."""
        now = request.now
        quiet_for = timedelta(minutes=request.quiet_minutes)
        window = timedelta(minutes=request.lane_window_minutes)
        stamps = [s for s in host.lane_stamps if s <= now + FUTURE_SLACK]
        if not any(now - window <= s for s in stamps):
            return None
        last = host.last_event_at
        if last is not None and now - last <= quiet_for:
            return None
        after = [s for s in stamps if last is None or s > last + EVENT_SLACK]
        if not after:
            return None
        first = min(after)
        return first if now - first >= quiet_for else None
