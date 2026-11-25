# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A landing controller that refuses three ticks in a row alarms once, naming the kind (OMN-26483)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.nodes.node_failure_alarm_router_effect.handler import (
    HandlerFailureAlarmRouter,
)
from omnimarket.nodes.node_failure_alarm_router_effect.models import (
    ModelFailureAlarmRequest,
)
from tests.nodes.node_failure_alarm_router_effect.helpers import (
    REFUSAL,
    FakePorts,
    base_rows,
    tick,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).parent / "fixtures" / "controller_ticks_2026_11_22_incident.json"
)


def _fire(ports: FakePorts, now: str):
    request = ModelFailureAlarmRequest(
        now=datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(UTC)
    )
    return HandlerFailureAlarmRouter(ports=ports).handle(request)


def test_controller_refusal_alarm_three_refused_ticks_write_one_alarm_and_one_msg() -> (
    None
):
    ports = FakePorts(base_rows())
    ports.ticks = [
        tick("2026-11-22T17:43:00Z", 1),
        tick("2026-11-22T17:48:00Z", 2, refusal=REFUSAL),
        tick("2026-11-22T17:53:00Z", 3, refusal=REFUSAL),
    ]
    assert _fire(ports, "2026-11-22T17:54:00Z").alarms == ()
    ports.ticks.append(tick("2026-11-22T17:58:00Z", 4, refusal=REFUSAL))
    result = _fire(ports, "2026-11-22T17:59:00Z")
    assert [a.cause for a in result.alarms] == ["controller-refusal:landing-controller"]
    alarm = result.alarms[0]
    assert alarm.kind == REFUSAL
    assert alarm.first_at == "2026-11-22T17:48:00Z"
    status = [r for _, rows in ports.appends for r in rows if " | STATUS | " in r]
    msgs = [r for _, rows in ports.appends for r in rows if " | MSG | " in r]
    assert len(status) == 1
    assert len(msgs) == 1
    assert REFUSAL in status[0]
    assert "2026-11-22T17:48:00Z" in status[0]
    assert REFUSAL in msgs[0]
    assert "2026-11-22T17:48:00Z" in msgs[0]
    # A fourth and fifth refused tick write none.
    for n, ts in ((5, "2026-11-22T18:03:00Z"), (6, "2026-11-22T18:08:00Z")):
        ports.ticks.append(tick(ts, n, refusal=REFUSAL))
        assert _fire(ports, ts).alarms == ()
    assert len(ports.appends) == 1
    assert len(ports.pushed) == 1


def test_controller_refusal_alarm_two_refused_ticks_do_not_alarm() -> None:
    ports = FakePorts(base_rows())
    ports.ticks = [
        tick("2026-11-22T17:48:00Z", 2, refusal=REFUSAL),
        tick("2026-11-22T17:53:00Z", 3, refusal=REFUSAL),
        tick("2026-11-22T17:58:00Z", 4),
        tick("2026-11-22T18:03:00Z", 5, refusal=REFUSAL),
    ]
    assert _fire(ports, "2026-11-22T18:04:00Z").alarms == ()
    assert ports.appends == []
    assert ports.pushed == []


def test_controller_refusal_resolved_first_deciding_tick_writes_one_resolved_row() -> (
    None
):
    ports = FakePorts(base_rows())
    ports.ticks = [
        tick("2026-11-22T17:48:00Z", 2, refusal=REFUSAL),
        tick("2026-11-22T17:53:00Z", 3, refusal=REFUSAL),
        tick("2026-11-22T17:58:00Z", 4, refusal=REFUSAL),
    ]
    _fire(ports, "2026-11-22T17:59:00Z")
    ports.ticks.append(tick("2026-11-22T18:51:00Z", 5, status="DEGRADED"))
    result = _fire(ports, "2026-11-22T18:52:00Z")
    assert len(result.resolved) == 1
    assert result.resolved[0].lasted_minutes == 63
    rows = [r for _, rows in ports.appends[1:] for r in rows]
    assert len(rows) == 1
    parsed = parse_row(rows[0])
    assert parsed is not None
    assert parsed.value("alarm") == "failure-cause-resolved"
    assert parsed.value("lasted_minutes") == "63"
    # A second deciding tick writes no second resolved row.
    ports.ticks.append(tick("2026-11-22T18:56:00Z", 6, status="DEGRADED"))
    assert _fire(ports, "2026-11-22T18:57:00Z").resolved == ()
    assert len(ports.appends) == 2


def test_controller_refusal_incident_replay_2026_11_22() -> None:
    """The 17:48Z to 18:51Z refusal run replayed tick by tick: one alarm, one resolved row, no repeats."""
    lines = json.loads(FIXTURE.read_text(encoding="utf-8"))["lines"]
    ports = FakePorts(base_rows())
    alarms = resolved = 0
    for end in range(1, len(lines) + 1):
        ports.ticks = lines[:end]
        stamp = json.loads(lines[end - 1])["ts"]
        result = _fire(ports, stamp)
        alarms += len(result.alarms)
        resolved += len(result.resolved)
    assert alarms == 1
    assert resolved == 1
    assert len(ports.pushed) == 1
    alarm_row = next(
        r for _, rows in ports.appends for r in rows if "alarm=failure-cause |" in r
    )
    assert "first_at=2026-11-22T17:48:" in alarm_row
    assert REFUSAL in alarm_row
    resolved_row = next(
        r for _, rows in ports.appends for r in rows if "failure-cause-resolved" in r
    )
    assert "lasted_minutes=" in resolved_row


def test_controller_refusal_drill_alarm_within_fifteen_minutes_of_first_refused_tick() -> (
    None
):
    """The AC4 drill's shape: an empty rows source makes every tick a refusal; the third tick alarms.

    The controller ticks about every five minutes, so the third refused tick is ten minutes after the
    first. The live drill on the controller host is read on 2026-11-29 (see the PR body for its command).
    """
    ports = FakePorts(base_rows())
    first = datetime(2026, 11, 29, 9, 0, tzinfo=UTC)
    alarm_at = None
    for n in range(6):
        at = first.replace(minute=5 * n)
        ports.ticks.append(tick(at.strftime("%Y-%m-%dT%H:%M:%SZ"), n, refusal=REFUSAL))
        if _fire(ports, at.strftime("%Y-%m-%dT%H:%M:%SZ")).alarms and alarm_at is None:
            alarm_at = at
    assert alarm_at is not None
    assert (alarm_at - first).total_seconds() <= 15 * 60
