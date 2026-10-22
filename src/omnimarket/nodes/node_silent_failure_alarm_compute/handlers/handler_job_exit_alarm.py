# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold launchd run counters into job-exit episodes without I/O (OMN-19998, OMN-17427).

An episode opens on a failed exit (``threshold`` nonzero runs in a row) or on a missed interval
(the run counter has not grown for ``missed_factor`` intervals, the interval read from launchd or
given for a calendar job in ``intervals``). One episode is open per job at a time, so a failing
job dispatches one lane, not one per tick. A job and cause that opened an episode inside
``cooldown_minutes`` waits, and at most ``max_opened`` episodes open in one tick; the rest open on
a later tick, in label order.
"""

from __future__ import annotations

from datetime import UTC, timedelta

from ..models import (
    ModelJobExitAlarmRequest,
    ModelJobExitAlarmResult,
    ModelJobExitEpisode,
    ModelJobExitReading,
    ModelJobExitStreak,
)
from ..models.model_job_exit_alarm import JobExitCause

_CLOSED = {
    "episode": None,
    "opened_at": None,
    "pushed": False,
    "dispatched": False,
    "cause": None,
}


def _missed(
    request: ModelJobExitAlarmRequest,
    reading: ModelJobExitReading,
    current: ModelJobExitStreak,
) -> int | None:
    """The interval in seconds when the job is overdue, else None."""
    interval = request.intervals.get(reading.label) or reading.interval_s
    if interval is None or current.last_run_at is None or reading.runs is None:
        return None
    overdue = request.now - current.last_run_at > timedelta(
        seconds=interval * request.missed_factor
    )
    return interval if overdue else None


class HandlerJobExitAlarm:
    """Count only observed new runs, open one episode per job and cause, escalate once."""

    def handle(self, request: ModelJobExitAlarmRequest) -> ModelJobExitAlarmResult:
        if not request.readable:
            return ModelJobExitAlarmResult(
                state="UNREADABLE",
                streaks=request.previous,
                failing=tuple(
                    sorted(
                        item.label
                        for item in request.previous
                        if item.streak >= request.threshold
                    )
                ),
            )

        streaks = {item.label: item for item in request.previous}
        opened: list[ModelJobExitEpisode] = []
        closed: list[str] = []
        push: list[ModelJobExitEpisode] = []
        suppressed: list[str] = []
        cells: list[str] = []
        alerts: list[str] = []
        now = request.now.astimezone(UTC)
        for reading in sorted(request.readings, key=lambda item: item.label):
            label = reading.label
            current = streaks.get(label, ModelJobExitStreak(label=label))
            advanced = (
                reading.runs is not None
                and reading.last_exit is not None
                and (current.runs is None or reading.runs > current.runs)
            )
            if advanced:
                current = current.model_copy(
                    update={
                        "runs": reading.runs,
                        "last_exit": reading.last_exit,
                        "streak": 0 if reading.last_exit == 0 else current.streak + 1,
                        "last_run_at": request.now,
                    }
                )
            elif (
                current.last_run_at is None
                and reading.runs is not None
                and reading.last_exit is not None
            ):
                # First sight of a job that has not run since: its clock starts now.
                current = current.model_copy(update={"last_run_at": request.now})
            if current.episode is not None and (
                (current.cause == "missed-interval" and advanced)
                or (current.cause != "missed-interval" and current.streak == 0)
            ):
                closed.append(current.episode)
                current = current.model_copy(update=_CLOSED)
            kind = request.lanes.get(label, "job-exit-fix")
            cause: JobExitCause | None = None
            interval = _missed(request, reading, current)
            if current.streak >= request.threshold:
                cause = "failed-exit"
            elif interval is not None:
                cause = "missed-interval"
            if cause is not None and current.episode is None:
                last = current.dispatched_at.get(cause)
                if last is not None and request.now - last < timedelta(
                    minutes=request.cooldown_minutes
                ):
                    suppressed.append(f"{label}:{cause}:cooldown")
                    cause = None
                elif len(opened) >= request.max_opened:
                    suppressed.append(f"{label}:{cause}:cap")
                    cause = None
            if cause is not None and current.episode is None:
                short = label.removeprefix("ai.omninode.")
                suffix = "-missed" if cause == "missed-interval" else ""
                episode_id = f"ep-{now:%Y%m%dT%H%MZ}-{short}{suffix}"
                current = current.model_copy(
                    update={
                        "episode": episode_id,
                        "opened_at": request.now,
                        "pushed": False,
                        "dispatched": False,
                        "cause": cause,
                        "dispatched_at": {**current.dispatched_at, cause: request.now},
                    }
                )
                opened.append(
                    ModelJobExitEpisode(
                        episode=episode_id,
                        label=label,
                        lane_kind=kind,
                        streak=current.streak,
                        last_exit=current.last_exit,
                        cause=cause,
                        stdout_path=reading.stdout_path,
                        stderr_path=reading.stderr_path,
                    )
                )
                head = (
                    "FRICTION | lane=silent-failure-alarm | ticket=OMN-19998 | "
                    "actor=launchd:silent-failure-alarm | class=job-exit | "
                    f"cause={cause} | episode={episode_id} | job={label} | "
                )
                if cause == "failed-exit":
                    cells.append(
                        f"{head}streak={current.streak} | exit={current.last_exit} | "
                        f"cost=est ~{request.threshold * 10} minutes "
                        "of a job exiting nonzero unseen | "
                        f"{label} exited nonzero on {current.streak} runs in a row "
                        f"(last exit {current.last_exit}); lane {kind} dispatched "
                        f"for episode {episode_id}"
                    )
                else:
                    assert current.last_run_at is not None
                    assert interval is not None
                    idle = int(
                        (request.now - current.last_run_at).total_seconds() // 60
                    )
                    cells.append(
                        f"{head}interval_s={interval} | "
                        f"since={current.last_run_at.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ} | "
                        f"cost=est ~{idle} minutes of a job not running unseen | "
                        f"{label} has not run for {idle} minutes (interval {interval} s); "
                        f"lane {kind} dispatched for episode {episode_id}"
                    )
            if (
                current.episode is not None
                and current.opened_at is not None
                and not current.pushed
                and current.episode not in request.cited_episodes
                and request.now - current.opened_at
                >= timedelta(minutes=request.push_after_minutes)
            ):
                push.append(
                    ModelJobExitEpisode(
                        episode=current.episode,
                        label=label,
                        lane_kind=kind,
                        streak=current.streak,
                        last_exit=current.last_exit,
                        cause=current.cause or "failed-exit",
                    )
                )
                what = (
                    f"has not run since {current.last_run_at.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"
                    if current.cause == "missed-interval"
                    and current.last_run_at is not None
                    else f"exited nonzero on {current.streak} runs in a row "
                    f"(last exit {current.last_exit})"
                )
                alerts.append(
                    f"JOB EXIT {label} {what}; episode {current.episode} opened "
                    f"{current.opened_at.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}; "
                    f"no CLAIM cites it after {request.push_after_minutes} minutes"
                )
                current = current.model_copy(update={"pushed": True})
            streaks[label] = current
        ordered = tuple(streaks[label] for label in sorted(streaks))
        failing = tuple(
            item.label
            for item in ordered
            if item.streak >= request.threshold or item.cause == "missed-interval"
        )
        return ModelJobExitAlarmResult(
            state="FAILING" if failing else "OK",
            streaks=ordered,
            opened=tuple(opened),
            closed=tuple(closed),
            push=tuple(push),
            failing=failing,
            suppressed=tuple(suppressed),
            friction_cells=tuple(cells),
            alert_text=tuple(alerts),
        )
