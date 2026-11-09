# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether the ticket closer has stopped running (OMN-20718).

THE INCIDENT. The closer exited 70 (ZERO_FLIP_WITH_CANDIDATES) on every run that flipped
nothing, so a working closer read as broken. Its launchd unit was then removed on 2026-10-27 and
nothing alarmed for 11.5 days: the only reading was a failed-run reading, and a unit that never
fires leaves no failed run.

WHAT IS DECIDED. The age of the closer's newest TERMINAL or run record against its declared
interval times the declared multiple (twice, by the operator's ruling of 2026-11-08). Past it is
STALE, whether the closer ran and failed or never ran: a run that closes zero tickets is a
working closer and is judged by its record, not by what it flipped. A unit the scheduler does not
list is named as the cause. No declared interval is UNCONFIGURED, an unreadable record source is
UNKNOWN; neither is ever LIVE.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.models.silent_failure_alarm.model_closer_staleness_alarm import (
    ModelCloserStalenessRequest,
    ModelCloserStalenessResult,
)

ALARM_LANE = "silent-failure-alarm"
TICKET = "OMN-20718"
FUTURE_SLACK_SECONDS = 300

__all__ = ["HandlerCloserStalenessAlarm", "ModelCloserStalenessResult"]


def _age(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d{hours:02d}h"
    return f"{hours}h{rest // 60:02d}m" if hours else f"{rest // 60}m"


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class HandlerCloserStalenessAlarm:
    """Age of the closer's newest record against its declared interval."""

    def handle(
        self, request: ModelCloserStalenessRequest
    ) -> ModelCloserStalenessResult:
        """Return LIVE, STALE, UNCONFIGURED or UNKNOWN, with the alarm cells when STALE."""
        if request.interval_seconds is None:
            return ModelCloserStalenessResult(alarm=False, state="UNCONFIGURED")
        if not request.records_readable:
            return ModelCloserStalenessResult(alarm=False, state="UNKNOWN")
        limit = request.interval_seconds * request.stale_multiple
        if request.last_record_at is None:
            seen = "no run record of the lane in the records read"
            age: int | None = None
        else:
            age = int((request.now - request.last_record_at).total_seconds())
            if age < -FUTURE_SLACK_SECONDS:
                seen = (
                    f"newest record {_stamp(request.last_record_at)} is in the future"
                )
            elif age <= limit:
                return ModelCloserStalenessResult(
                    alarm=False,
                    state="LIVE",
                    age_seconds=max(age, 0),
                    limit_seconds=limit,
                )
            else:
                seen = (
                    f"newest record {_stamp(request.last_record_at)} is {_age(age)} old"
                )
        unit = (
            f"; the scheduler does not list {request.unit_label} (booted out or removed)"
            if request.unit_loaded is False
            else ""
        )
        cause = (
            f"{request.lane} has not run for more than {request.stale_multiple}x its "
            f"{request.interval_seconds}s interval ({_age(limit)}): {seen}{unit}"
        )
        fix = (
            f"run onex-host-install for {request.unit_label} (--status, then --kickstart), "
            f"then read the lane's next TERMINAL"
        )
        cells = (
            f"FRICTION | lane={ALARM_LANE} | ticket={TICKET} | actor=launchd:{ALARM_LANE} | "
            f"class=closer-staleness | closer={request.lane} | "
            f"limit_seconds={limit} | "
            f"cost=est ~{max((age or limit) - limit, 0) // 3600} hours past the bound with the "
            f"closer not running | {cause}; fix: {fix}"
        )
        return ModelCloserStalenessResult(
            alarm=True,
            state="STALE",
            age_seconds=age,
            limit_seconds=limit,
            cause=cause,
            fix=fix,
            friction_cells=cells,
            alert_text=f"CLOSER STALE {cause}; fix: {fix}",
        )
