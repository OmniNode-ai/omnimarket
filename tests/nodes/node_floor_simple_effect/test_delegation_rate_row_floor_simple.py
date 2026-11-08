# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor shows the delegation completion rate under piece 15 with its read time (OMN-23108).

The row is the one the completion-rate compute node builds: the handler runs over planted hours and its ledger row goes into an
in-memory ledger that the floor tick reads, so the writer and the reader are held to each other. The failing controls are a row of
the same lane that the rule does not match (a drill), an unreadable-rows reading and a stale row.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorShadowLine,
    ModelFloorSimple,
    ModelFloorSimpleOverlay,
    ModelFloorSimpleTickRequest,
)
from omnimarket.nodes.node_delegation_completion_rate_compute.handlers.handler_delegation_completion_rate import (
    HandlerDelegationCompletionRate,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    read_ledger_facts,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_delegation_completion_rate_compute.completion_rate_support import (
    NOW,
    above_bound_hour,
    below_bound_hour,
    request,
)
from tests.nodes.node_floor_simple_effect.support import (
    OVERLAYS,
    build_home,
    environ,
    overlay_text,
)

pytestmark = pytest.mark.unit

PIECE = 15


def floor_overlay() -> ModelFloorSimpleOverlay:
    return ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )


def run_tick(home: Path, ledger: InMemoryLedger, *, at: datetime) -> ModelFloorSimple:
    env = {**environ(home), "ONEX_SKILL_OVERLAY_ROOTS": str(OVERLAYS)}
    handler = HandlerFloorSimpleTick(
        sink=InMemoryEmissionSink(),
        clock=lambda: at,
        served_alarm_ids=lambda: None,
        ledger_source=ledger,
    )
    result = handler.run(
        ModelFloorSimpleTickRequest(home=str(home), now=at), environ=env
    )
    assert result.ok
    return ModelFloorSimple.model_validate_json(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text(
            encoding="utf-8"
        )
    )


def write_reading(ledger: InMemoryLedger, **over: object) -> str:
    row = HandlerDelegationCompletionRate().handle(request(**over)).ledger_row
    appended = ledger.append(row + "\n", f"rate-{len(ledger.lines)}")
    assert appended.ok
    return row


def lines(floor: ModelFloorSimple) -> tuple[ModelFloorShadowLine, ...]:
    return next(p for p in floor.pieces if p.piece == PIECE).shadows


def test_the_floor_row_shows_the_hour_with_its_verdict_and_read_time(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    write_reading(ledger, events=above_bound_hour())
    floor = run_tick(build_home(tmp_path), ledger, at=NOW + timedelta(minutes=20))

    (line,) = lines(floor)
    assert line.label == "Delegation completion rate"
    assert line.text == (
        "verdict WORKING, completions an hour 136, by the deployed lane 100, failed share 0.0000"
    )
    assert line.age_s == 1200
    assert line.max_age_s == 7200
    assert line.stale is False
    assert [p.shadows for p in floor.pieces if p.piece != PIECE] == [()] * 17
    served = floor.model_dump_json()
    assert "Delegation completion rate" in served


def test_a_below_bound_hour_shows_not_working_on_the_floor_row(tmp_path: Path) -> None:
    ledger = InMemoryLedger()
    write_reading(ledger, events=below_bound_hour())
    floor = run_tick(build_home(tmp_path), ledger, at=NOW + timedelta(minutes=5))
    (line,) = lines(floor)
    assert line.text.startswith(
        "verdict NOT_WORKING, completions an hour 14, by the deployed lane 0"
    )


def test_the_newest_reading_is_the_one_shown(tmp_path: Path) -> None:
    ledger = InMemoryLedger()
    write_reading(ledger, events=below_bound_hour(), now=NOW - timedelta(hours=1))
    write_reading(ledger, events=above_bound_hour())
    floor = run_tick(build_home(tmp_path), ledger, at=NOW + timedelta(minutes=1))
    (line,) = lines(floor)
    assert "verdict WORKING" in line.text


def test_a_reading_past_its_declared_age_is_marked_stale(tmp_path: Path) -> None:
    ledger = InMemoryLedger()
    write_reading(ledger, events=above_bound_hour())
    floor = run_tick(build_home(tmp_path), ledger, at=NOW + timedelta(hours=3))
    (line,) = lines(floor)
    assert line.stale is True
    assert line.age_s == 3 * 3600


def test_failing_control_unreadable_rows_show_their_outcome_not_a_zero(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    write_reading(ledger, events=(), events_unread_reason="delegation_events-down")
    floor = run_tick(build_home(tmp_path), ledger, at=NOW + timedelta(minutes=5))
    (line,) = lines(floor)
    assert line.text == "not read: source-unreadable"


def test_failing_control_a_drill_row_draws_no_line(tmp_path: Path) -> None:
    ledger = InMemoryLedger()
    row = write_reading(ledger, events=above_bound_hour())
    ledger.lines[:] = [row.replace("drill=no", "drill=yes")]
    floor = run_tick(build_home(tmp_path), ledger, at=NOW + timedelta(minutes=5))
    assert lines(floor) == ()


def test_failing_control_no_row_on_the_ledger_draws_no_line(tmp_path: Path) -> None:
    floor = run_tick(build_home(tmp_path), InMemoryLedger(), at=NOW)
    assert lines(floor) == ()


def test_failing_control_an_unreadable_ledger_is_an_error_never_no_rows() -> None:
    ledger = InMemoryLedger()
    ledger.read_error = "ledger-down"
    read = read_ledger_facts(ledger, floor_overlay(), NOW)
    assert read.facts is None
    assert read.error is not None


def test_a_reading_never_changes_the_colour_of_the_piece(tmp_path: Path) -> None:
    without = run_tick(build_home(tmp_path / "a"), InMemoryLedger(), at=NOW)
    ledger = InMemoryLedger()
    write_reading(ledger, events=below_bound_hour())
    with_row = run_tick(build_home(tmp_path / "b"), ledger, at=NOW)
    before = next(p for p in without.pieces if p.piece == PIECE)
    after = next(p for p in with_row.pieces if p.piece == PIECE)
    assert (after.status, after.draw, after.reading_state) == (
        before.status,
        before.draw,
        before.reading_state,
    )
