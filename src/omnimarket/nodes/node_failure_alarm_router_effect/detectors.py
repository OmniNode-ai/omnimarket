# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failure causes read from pr-create refusal rows, seam deadline readings and delegation STATUS rows (OMN-26216).

Three failure signals were written as rows that nothing read: the dev work-ledger query answerer timed out from
12:14Z to about 20:00Z on 2026-11-23 while its seam probe read NOT_WORKING every five minutes, four lanes hit a pr-create
refusal each, and the delegation lane failed every claude-sonnet-5-5 call with provider_auth_failed from 05:18Z. Each
reader here reads rows only; the thresholds come from the contract's ``config.failure_alarm_router`` (``rules.py``).
A cause reads the run that is current: the rows since the subject last worked, so a recovery followed by a new failure is a new cause.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from omnimarket.handlers.ledger_row_grammar import Row
from omnimarket.handlers.pr_create_refusal_causes import CAUSES, KINDS, REFUSAL
from omnimarket.nodes.node_failure_alarm_router_effect.causes import (
    ROUTER_LANE,
    Cause,
    OpenCause,
    cell,
    parse_stamp,
)
from omnimarket.nodes.node_failure_alarm_router_effect.rules import (
    ModelFailureAlarmRules,
)

PR_CREATE_REFUSAL = "pr-create-refusal"
SEAM_NOT_WORKING = "seam-not-working"
SEAM_OVER_BUDGET = "seam-over-budget"
DELEGATION_STREAK = "delegation-model-failing"
DRILL = "drill"

_SEAM_CELL = re.compile(
    r"^seam_(?P<seam>[^=\s]+)=(?P<verdict>WORKING|NOT_WORKING):(?P<crossing>[a-z-]+):"
    r"(?P<ms>none|\d+)ms/(?P<deadline>\d+)ms$"
)
_COMPLETED = "completed"
_FAILED = "failed"


@dataclass(frozen=True)
class _Seam:
    stamp: str
    seam: str
    working: bool
    crossing: str
    ms: int | None
    deadline_ms: int
    drill: bool


def _in_window(row: Row, since: datetime) -> bool:
    return row.token("lane") != ROUTER_LANE and parse_stamp(row.stamp) >= since


def refusal_causes(rows: list[Row], since: datetime) -> list[Cause]:
    """One cause per refusal cause named in a STATUS or TERMINAL row, first seen at its earliest row."""
    found: dict[str, Cause] = {}
    for row in rows:
        if row.row_type not in KINDS or not _in_window(row, since):
            continue
        if not REFUSAL.search(row.text):
            continue
        for declared in CAUSES:
            if declared.pattern.search(row.text):
                cause = Cause(
                    PR_CREATE_REFUSAL,
                    declared.name,
                    row.stamp,
                    cell(declared.says),
                )
                held = found.get(cause.key)
                if held is None or parse_stamp(cause.first_at) < parse_stamp(
                    held.first_at
                ):
                    found[cause.key] = cause
                break
    return list(found.values())


def _seam_readings(rows: list[Row], rules: ModelFailureAlarmRules) -> list[_Seam]:
    readings: list[_Seam] = []
    for row in rows:
        if (
            row.row_type != "STATUS"
            or row.value("src") != rules.seam_deadline.source_cell
        ):
            continue
        drill = row.value("drill") == "yes"
        for part in row.cells:
            match = _SEAM_CELL.match(part.strip())
            if match:
                ms = match["ms"]
                readings.append(
                    _Seam(
                        stamp=row.stamp,
                        seam=match["seam"],
                        working=match["verdict"] == "WORKING",
                        crossing=match["crossing"],
                        ms=None if ms == "none" else int(ms),
                        deadline_ms=int(match["deadline"]),
                        drill=drill,
                    )
                )
    readings.sort(key=lambda r: r.stamp)
    return readings


def _over_budget(reading: _Seam, rules: ModelFailureAlarmRules) -> bool:
    return (
        reading.working
        and reading.ms is not None
        and reading.ms > rules.seam_deadline.latency_budget_ms
    )


def seam_causes(
    rows: list[Row], since: datetime, rules: ModelFailureAlarmRules
) -> list[Cause]:
    """A seam that reads NOT_WORKING, or answers slower than the latency budget, in its current run of readings.

    A drill reading (``drill=yes``) raises its own cause and never joins the operational run.
    """
    readings = [r for r in _seam_readings(rows, rules) if parse_stamp(r.stamp) >= since]
    budget = rules.seam_deadline.latency_budget_ms
    found: list[Cause] = []
    for reading in readings:
        if reading.drill and not reading.working:
            found.append(
                Cause(
                    SEAM_NOT_WORKING,
                    f"{reading.seam}:{DRILL}@{reading.stamp}",
                    reading.stamp,
                    cell(
                        f"drill: {reading.crossing}, deadline {reading.deadline_ms} ms"
                    ),
                )
            )
    operational = [r for r in readings if not r.drill]
    for seam in dict.fromkeys(r.seam for r in operational):
        mine = [r for r in operational if r.seam == seam]
        newest = mine[-1]
        if not newest.working:
            run = _run(mine, lambda r: not r.working)
            took = f" after {newest.ms} ms" if newest.ms is not None else ""
            found.append(
                Cause(
                    SEAM_NOT_WORKING,
                    seam,
                    run[0].stamp,
                    cell(
                        f"{newest.crossing}{took}, deadline {newest.deadline_ms} ms, "
                        f"{len(run)} reading(s) since"
                    ),
                )
            )
        elif _over_budget(newest, rules):
            run = _run(mine, lambda r: _over_budget(r, rules))
            found.append(
                Cause(
                    SEAM_OVER_BUDGET,
                    seam,
                    run[0].stamp,
                    cell(
                        f"answered in {newest.ms} ms, over the {budget} ms latency budget, "
                        f"{len(run)} reading(s) since"
                    ),
                )
            )
    return found


def _run(readings: list[_Seam], in_run: Callable[[_Seam], bool]) -> list[_Seam]:
    """The newest unbroken run of readings for which ``in_run`` holds, oldest first."""
    run: list[_Seam] = []
    for reading in reversed(readings):
        if not in_run(reading):
            break
        run.append(reading)
    run.reverse()
    return run


def delegation_causes(
    rows: list[Row], since: datetime, now: datetime, rules: ModelFailureAlarmRules
) -> list[Cause]:
    """A model with ``min_failed_rows`` failed rows of one failure class and no completed row in ``quiet_minutes``."""
    streak = rules.delegation_streak
    unnamed = {name.casefold() for name in streak.unnamed_models}
    by_model: dict[str, list[Row]] = {}
    for row in rows:
        model = row.value("model")
        if (
            row.row_type == "STATUS"
            and row.value("src") == streak.source_cell
            and model
            and model.casefold() not in unnamed
            and _in_window(row, since)
        ):
            by_model.setdefault(model, []).append(row)
    quiet = timedelta(minutes=streak.quiet_minutes)
    found: list[Cause] = []
    for model, model_rows in sorted(by_model.items()):
        ordered = sorted(model_rows, key=lambda r: r.stamp)
        completed = [r for r in ordered if r.value("outcome") == _COMPLETED]
        last_completed = completed[-1].stamp if completed else ""
        if completed and now - parse_stamp(last_completed) < quiet:
            continue
        failed: dict[str, list[Row]] = {}
        for row in ordered:
            if row.value("outcome") == _FAILED and row.stamp > last_completed:
                failure_class = (
                    row.value("last_attempt_failure_class")
                    or row.value("reason")
                    or "unclassified"
                )
                failed.setdefault(failure_class, []).append(row)
        for failure_class, hits in sorted(failed.items()):
            if len(hits) < streak.min_failed_rows:
                continue
            found.append(
                Cause(
                    DELEGATION_STREAK,
                    f"{model}:{failure_class}",
                    hits[0].stamp,
                    cell(
                        f"{len(hits)} failed delegation rows ({failure_class}) and no completed row "
                        f"of {model} in {streak.quiet_minutes} minutes, newest {hits[-1].stamp}"
                    ),
                )
            )
    return found


def extra_causes(
    rows: list[Row], since: datetime, now: datetime, rules: ModelFailureAlarmRules
) -> list[Cause]:
    return [
        *refusal_causes(rows, since),
        *seam_causes(rows, since, rules),
        *delegation_causes(rows, since, now, rules),
    ]


def resolution(
    opened: OpenCause, rows: list[Row], rules: ModelFailureAlarmRules
) -> datetime | None:
    """When an open refusal, seam or delegation cause ended, or None while it has not."""
    first = parse_stamp(opened.first_at)
    if opened.failure_class in {SEAM_NOT_WORKING, SEAM_OVER_BUDGET}:
        seam = opened.subject.split(":", 1)[0]
        for reading in _seam_readings(rows, rules):
            if (
                reading.seam != seam
                or reading.drill
                or parse_stamp(reading.stamp) <= first
            ):
                continue
            if reading.working and (
                opened.failure_class == SEAM_NOT_WORKING
                or not _over_budget(reading, rules)
            ):
                return parse_stamp(reading.stamp)
            if opened.failure_class == SEAM_OVER_BUDGET and not reading.working:
                return parse_stamp(reading.stamp)
        return None
    if opened.failure_class == DELEGATION_STREAK:
        model = opened.subject.split(":", 1)[0]
        for row in sorted(rows, key=lambda r: r.stamp):
            if (
                row.row_type == "STATUS"
                and row.value("src") == rules.delegation_streak.source_cell
                and row.value("model") == model
                and row.value("outcome") == _COMPLETED
                and parse_stamp(row.stamp) > first
            ):
                return parse_stamp(row.stamp)
    return None


__all__ = [
    "DELEGATION_STREAK",
    "DRILL",
    "PR_CREATE_REFUSAL",
    "SEAM_NOT_WORKING",
    "SEAM_OVER_BUDGET",
    "delegation_causes",
    "extra_causes",
    "refusal_causes",
    "resolution",
    "seam_causes",
]
