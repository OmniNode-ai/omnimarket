# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failure causes read from ledger rows and controller tick lines (OMN-26483).

A cause key is the failure class plus its subject. Nothing here writes: it reads rows and says which causes
are active, which are already open (an alarm row with no later resolved row) and which open ones have ended.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from omnimarket.handlers.ledger_row_grammar import Row

ROUTER_LANE = "failure-alarm-router"
CONTROLLER_REFUSAL = "controller-refusal"
LEDGER_REFUSED = "ledger-refused"
MAIN_RED = "main-red"
HELD_ROLL = "held-roll-command"
ALARM_CELL = "failure-cause"
RESOLVED_CELL = "failure-cause-resolved"

# A lane that ended with the ledger refusing its rows (exit 70): `status=ledger-refused` on a receipt, or the
# two words within one cell of each other.
LEDGER_REFUSED_RE = re.compile(
    r"status=ledger-refused"
    r"|ledger-refused[^|]{0,80}exit(?:_code)?[= ]\s*70"
    r"|exit(?:_code)?[= ]\s*70[^|]{0,80}ledger-refused"
)
HELD_ROLL_GAP = (
    "held roll commands live in the roll gate's durable store (gate-state.json on the lab host), and no ledger "
    "row carries a held command or its age, so an alarm for one held over 30 minutes cannot be raised from "
    "ledger rows; the gate needs to write a row when it holds a command"
)


@dataclass(frozen=True)
class Cause:
    failure_class: str
    subject: str
    first_at: str
    kind: str

    @property
    def key(self) -> str:
        return f"{self.failure_class}:{self.subject}"


@dataclass(frozen=True)
class OpenCause:
    key: str
    failure_class: str
    subject: str
    first_at: str


@dataclass(frozen=True)
class Tick:
    at: datetime
    refused: bool
    kind: str


def cell(value: object, limit: int = 200) -> str:
    """One ledger cell: no pipe, no line break, bounded."""
    return " ".join(str(value).replace("|", "/").split())[:limit]


def stamp_of(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def parse_ticks(lines: list[str]) -> list[Tick]:
    """The controller's tick lines in time order; a line that is not a tick record is skipped."""
    ticks: list[Tick] = []
    for line in lines:
        try:
            record = json.loads(line)
            at = parse_stamp(record["ts"])
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if not isinstance(record, dict):
            continue
        status = str(record.get("status", ""))
        refusal = record.get("refusal")
        refused = (isinstance(refusal, str) and bool(refusal.strip())) or (
            status.upper() == "REFUSED"
        )
        kind = ""
        for field in (refusal, record.get("detail"), record.get("reason"), status):
            if isinstance(field, str) and field.strip():
                kind = field.strip().splitlines()[0]
                break
        ticks.append(Tick(at=at, refused=refused, kind=cell(kind)))
    ticks.sort(key=lambda tick: tick.at)
    return ticks


def controller_cause(ticks: list[Tick], subject: str, threshold: int) -> Cause | None:
    """The cause when the newest ``threshold`` or more ticks in a row were refused."""
    run: list[Tick] = []
    for tick in reversed(ticks):
        if not tick.refused:
            break
        run.append(tick)
    if len(run) < threshold:
        return None
    run.reverse()
    return Cause(CONTROLLER_REFUSAL, subject, stamp_of(run[0].at), run[0].kind)


def row_causes(rows: list[Row], since: datetime) -> list[Cause]:
    """Ledger-refused lanes and red default branches, read from rows inside the look-back window."""
    found: dict[str, Cause] = {}
    for row in rows:
        if row.token("lane") == ROUTER_LANE:
            continue
        if parse_stamp(row.stamp) < since:
            continue
        if row.row_type in {"TERMINAL", "STATUS"} and LEDGER_REFUSED_RE.search(
            row.text
        ):
            lane = row.token("lane")
            if lane:
                cause = Cause(
                    LEDGER_REFUSED,
                    lane,
                    row.stamp,
                    "lane ended ledger-refused (exit 70)",
                )
                found.setdefault(cause.key, cause)
        elif row.row_type == "STATUS" and row.value("alarm") == MAIN_RED:
            repo = row.value("repo") or "unknown-repo"
            branch = row.value("branch") or "unknown-branch"
            head = (row.value("head") or "unknown-head")[:12]
            kind = (
                f"{row.value('check') or 'check'} is {row.value('conclusion') or 'red'}"
            )
            cause = Cause(MAIN_RED, f"{repo}:{branch}:{head}", row.stamp, cell(kind))
            found.setdefault(cause.key, cause)
    return list(found.values())


def open_and_resolved(
    rows: list[Row],
) -> tuple[dict[str, OpenCause], dict[str, datetime]]:
    """Causes with an alarm row and no later resolved row, and when each key was last resolved."""
    open_causes: dict[str, OpenCause] = {}
    resolved_at: dict[str, datetime] = {}
    for row in rows:
        if row.token("lane") != ROUTER_LANE or row.value("src") != "alarm":
            continue
        key = row.value("cause")
        if not key:
            continue
        if row.value("alarm") == ALARM_CELL:
            open_causes[key] = OpenCause(
                key=key,
                failure_class=row.value("class") or key.partition(":")[0],
                subject=row.value("subject") or key.partition(":")[2],
                first_at=row.value("first_at") or row.stamp,
            )
        elif row.value("alarm") == RESOLVED_CELL:
            open_causes.pop(key, None)
            at = row.value("resolved_at") or row.stamp
            resolved_at[key] = parse_stamp(at)
    return open_causes, resolved_at


def resolution(
    open_cause: OpenCause, rows: list[Row], ticks: list[Tick]
) -> datetime | None:
    """When the open cause ended, or None while it has not (or cannot be read to have)."""
    first = parse_stamp(open_cause.first_at)
    if open_cause.failure_class == CONTROLLER_REFUSAL:
        for tick in ticks:
            if tick.at > first and not tick.refused:
                return tick.at
        return None
    if open_cause.failure_class == LEDGER_REFUSED:
        for row in rows:
            if (
                row.row_type == "CLAIM"
                and row.token("lane") == open_cause.subject
                and parse_stamp(row.stamp) > first
            ):
                return parse_stamp(row.stamp)
    return None


def lasted_minutes(first_at: str, resolved: datetime) -> int:
    return max(0, round((resolved - parse_stamp(first_at)) / timedelta(minutes=1)))


def live_orchestrator(rows: list[Row], pattern: str) -> tuple[str, str] | None:
    """The live orchestrator lane and how it was found, or None.

    Live means an open CLAIM (a TERMINAL of the lane closes it). A lane that holds no CLAIM of its own is
    still live when an open CLAIM names it in ``parent=`` (the session lane of 2026-11-22 wrote no CLAIM, and
    its children's CLAIMs name it). A lane whose newest row of the two kinds is a TERMINAL is closed.
    """
    claims: dict[str, str] = {}
    parents: dict[str, str] = {}
    closed: set[str] = set()
    for row in rows:
        lane = row.token("lane")
        if not lane:
            continue
        if row.row_type == "CLAIM":
            claims[lane] = row.stamp
            closed.discard(lane)
            parent = row.token("parent")
            if parent:
                parents[lane] = parent
        elif row.row_type == "TERMINAL":
            claims.pop(lane, None)
            parents.pop(lane, None)
            closed.add(lane)
    named: dict[str, tuple[str, str]] = {}
    for lane, at in claims.items():
        named[lane] = max(named.get(lane, ("", "")), (at, "claim-lane"))
        parent = parents.get(lane)
        if parent is not None:
            named[parent] = max(named.get(parent, ("", "")), (at, "claim-parent"))
    live = {
        lane: found
        for lane, found in named.items()
        if re.match(pattern, lane) is not None and lane not in closed
    }
    if not live:
        return None
    lane = max(live, key=lambda name: (live[name][0], name))
    return lane, live[lane][1]
