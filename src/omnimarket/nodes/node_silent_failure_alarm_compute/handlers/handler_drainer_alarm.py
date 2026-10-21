# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether the hook-emit drainer is dead and a reinstall is the safe answer (OMN-20708).

THE INCIDENT. The h200 drainer that feeds the database work ledger exited at 2026-10-15T15:13:22Z.
launchd could not respawn it (exit 78): the interpreter venv it names had been removed. Nothing
alarmed for about 20 hours, because the quiet-host alarm only speaks of a host that ran lanes, and
the drainer's own status file says nothing once the process is gone.

WHAT IS DECIDED. The drainer is DEAD when its supervisor job is not running, when the interpreter
the installed job names is missing, when its status file is absent, or when its last confirmed
publish is older than ``stale_minutes`` while it has a backlog to publish or has stopped cycling.
A drainer that cycles on an empty journal publishes nothing and is idle, not dead.

WHEN A REINSTALL IS SAFE. The installer is idempotent and a singleton lock keeps a restart from
producing a second publisher, so a missing job, a missing interpreter, a missing status file and a
stopped cycle all call for it. A drainer that cycles and cannot publish is a broker problem a
restart does not fix, so that one is raised and left alone.

A supervisor that could not be asked is UNKNOWN, never LIVE: the alarm that checks the drainer
must not pass because the check itself failed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from ..models import (
    ModelDrainerObservation,
    ModelDrainerRequest,
    ModelDrainerResult,
)

LANE = "silent-failure-alarm"
TICKET = "OMN-20708"
INSTALLER = "omniclaude/scripts/install-hook-emit-drainer.sh"
PRESERVE_BACKLOG = "ONEX_HOOK_EMIT_PRESERVE_BACKLOG=1"
FIX = (
    f"{PRESERVE_BACKLOG} bash {INSTALLER} on the named host, then bash {INSTALLER} --status; "
    f"reinstall without {PRESERVE_BACKLOG} once the backlog has drained"
)
BROKER_FIX = (
    f"the drainer cycles and cannot publish: check the broker route from the named host, then "
    f"bash {INSTALLER} --status"
)

_Outcome = Literal["none", "healed", "failed", "skipped"]

JOB_NOT_RUNNING = "job-not-running"
INTERPRETER_MISSING = "interpreter-missing"
STATUS_ABSENT = "status-absent"
PUBLISH_STALE = "publish-stale"

__all__ = ["HandlerDrainerAlarm", "ModelDrainerResult", "reasons_for"]


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02d}h"
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def _cycle_stalled(
    obs: ModelDrainerObservation, now: datetime, limit: timedelta
) -> bool:
    return obs.last_cycle_at is None or now - obs.last_cycle_at > limit


def reasons_for(
    obs: ModelDrainerObservation, now: datetime, stale_minutes: int
) -> tuple[str, ...]:
    """The reasons one read shows the drainer dead; empty when it is live or cannot be told."""
    limit = timedelta(minutes=stale_minutes)
    found: list[str] = []
    if obs.job_state == "not-running":
        found.append(JOB_NOT_RUNNING)
    if obs.interpreter_present is False:
        found.append(INTERPRETER_MISSING)
    if not obs.status_readable:
        found.append(STATUS_ABSENT)
        return tuple(found)
    published_late = obs.last_publish_at is None or now - obs.last_publish_at > limit
    if published_late and (obs.journal_depth > 0 or _cycle_stalled(obs, now, limit)):
        found.append(PUBLISH_STALE)
    return tuple(found)


class HandlerDrainerAlarm:
    """Is the hook-emit drainer publishing, and may the tick reinstall it."""

    def handle(self, request: ModelDrainerRequest) -> ModelDrainerResult:
        """Return LIVE, DEAD or UNKNOWN, with the alarm cells and the repair outcome when DEAD."""
        now, before = request.now, request.before
        reasons = reasons_for(before, now, request.stale_minutes)
        if not reasons:
            unknown = before.job_state == "unknown"
            return ModelDrainerResult(
                alarm=False, state="UNKNOWN" if unknown else "LIVE"
            )
        limit = timedelta(minutes=request.stale_minutes)
        cycling = not _cycle_stalled(before, now, limit) and before.status_readable
        broker_only = reasons == (PUBLISH_STALE,) and cycling
        repair_safe = not broker_only
        fix = BROKER_FIX if broker_only else FIX
        outcome = self._outcome(request, repair_safe)
        findings = self._findings(request, reasons)
        repair_note = self._repair_note(request, outcome)
        names = ",".join(reasons)
        cells = (
            f"FRICTION | lane={LANE} | ticket={TICKET} | actor=launchd:{LANE} | "
            f"class=drainer-dead | hosts={request.host} | reasons={names} | repair={outcome} | "
            f"cost=est ~{self._minutes(request)} minutes of hook events not reaching the "
            f"database ledger | {findings}; {repair_note}; fix: {fix}"
        )
        return ModelDrainerResult(
            alarm=True,
            state="DEAD",
            reasons=reasons,
            repair_safe=repair_safe,
            repair_outcome=outcome,
            fix=fix,
            friction_cells=cells,
            alert_text=(
                f"HOOK-EMIT DRAINER DEAD {request.host}: {findings}; {repair_note}; fix: {fix}"
            ),
        )

    @staticmethod
    def _outcome(request: ModelDrainerRequest, repair_safe: bool) -> _Outcome:
        repair = request.repair
        if repair is None:
            return "skipped" if not repair_safe else "none"
        if not repair.succeeded:
            return "failed"
        # A drainer reinstalled seconds ago has not yet had a cycle to confirm a publish; what
        # proves the reinstall is a running job on a present interpreter.
        after = repair.after
        running = (
            after.job_state == "running" and after.interpreter_present is not False
        )
        return "healed" if running else "failed"

    @staticmethod
    def _repair_note(request: ModelDrainerRequest, outcome: str) -> str:
        if outcome == "healed":
            return f"reinstalled with {PRESERVE_BACKLOG}; the job is running again"
        if outcome == "failed":
            detail = request.repair.detail if request.repair else "no detail"
            return f"reinstall with {PRESERVE_BACKLOG} did not bring it back: {detail}"
        if outcome == "skipped":
            return "no reinstall: a restart does not fix a drainer that cycles and cannot publish"
        return "no reinstall attempted this tick"

    @staticmethod
    def _findings(request: ModelDrainerRequest, reasons: tuple[str, ...]) -> str:
        before, now = request.before, request.now
        parts: list[str] = []
        if JOB_NOT_RUNNING in reasons:
            parts.append(f"job not running ({before.job_detail or 'no detail'})")
        if INTERPRETER_MISSING in reasons:
            parts.append(f"interpreter missing: {before.interpreter_path}")
        if STATUS_ABSENT in reasons:
            parts.append("drainer status file absent or unreadable")
        if PUBLISH_STALE in reasons:
            last = (
                "never confirmed a publish"
                if before.last_publish_at is None
                else f"last publish {_stamp(before.last_publish_at)}"
                f" ({_age(now - before.last_publish_at)} ago)"
            )
            parts.append(f"{last}; {before.journal_depth} record(s) queued")
        return "; ".join(parts)

    @staticmethod
    def _minutes(request: ModelDrainerRequest) -> int:
        before = request.before
        anchor = before.last_publish_at or before.last_cycle_at
        floor = request.stale_minutes
        if anchor is None:
            return floor
        return max(int((request.now - anchor).total_seconds() // 60), floor)
