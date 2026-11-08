# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor shows piece 1's shadow results with their ages (OMN-22902).

The rows are the ones the code graph's shadow handler writes: the handler runs over an in-memory ledger and the floor tick
reads that ledger, so the writer and the reader are held to each other. The failing control is a row the rule does not match
(a drill) and an unreadable ledger: neither draws a shadow line, and a shadow line never changes the piece's colour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorShadowLine,
    ModelFloorSimple,
    ModelFloorSimpleOverlay,
    ModelFloorSimpleTickRequest,
)
from omnimarket.models.model_code_graph import EnumGraphFault, ModelShadowCommand
from omnimarket.nodes.node_code_graph_effect.adapters.pull_request_sources import (
    InMemoryPullRequestSource,
)
from omnimarket.nodes.node_code_graph_effect.handlers.shadow_check import (
    CodeGraphShadowCheck,
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
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)
from tests.nodes.node_code_graph_effect.code_graph_support import HOST, overlay
from tests.nodes.node_floor_simple_effect.support import (
    OVERLAYS,
    build_home,
    environ,
    overlay_text,
)
from tests.unit.nodes.node_code_graph_effect.pr_support import (
    CLEAN_SHA,
    PLANTED,
    PLANTED_SHA,
    WIRED,
    memory_source,
    pull_request,
)

pytestmark = pytest.mark.unit

WROTE = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)


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


def write_shadow_row(
    ledger: InMemoryLedger,
    *,
    at: datetime = WROTE,
    fault: EnumGraphFault = EnumGraphFault.NONE,
) -> None:
    result = CodeGraphShadowCheck(
        overlay=overlay(),
        host=HOST,
        source=memory_source({CLEAN_SHA: WIRED, PLANTED_SHA: PLANTED}),
        pull_requests=InMemoryPullRequestSource(
            [pull_request(11, CLEAN_SHA), pull_request(12, PLANTED_SHA)]
        ),
        appender=ledger,
        clock=lambda: at,
    ).handle(ModelShadowCommand(correlation_id="floor-shadow", fault=fault))
    assert result.ledger == "written"


def piece_one(floor: ModelFloorSimple) -> tuple[ModelFloorShadowLine, ...]:
    return next(p for p in floor.pieces if p.piece == 1).shadows


def test_the_floor_shows_the_newest_shadow_row_with_its_age(tmp_path: Path) -> None:
    ledger = InMemoryLedger()
    write_shadow_row(ledger, at=WROTE - timedelta(hours=1))
    write_shadow_row(ledger, at=WROTE)
    floor = run_tick(build_home(tmp_path), ledger, at=WROTE + timedelta(minutes=20))

    (line,) = piece_one(floor)
    assert line.label == "Pull request check in shadow"
    assert line.text == "checked 2, would refuse 1, unreadable 0"
    assert line.age_s == 1200
    assert line.max_age_s == 3600
    assert line.stale is False
    assert [p.shadows for p in floor.pieces if p.piece != 1] == [()] * 17


def test_a_shadow_row_past_its_declared_age_is_marked_stale(tmp_path: Path) -> None:
    ledger = InMemoryLedger()
    write_shadow_row(ledger)
    floor = run_tick(build_home(tmp_path), ledger, at=WROTE + timedelta(hours=3))
    (line,) = piece_one(floor)
    assert line.stale is True
    assert line.age_s == 3 * 3600


def test_a_shadow_row_never_changes_the_colour_of_the_piece(tmp_path: Path) -> None:
    without = run_tick(build_home(tmp_path / "a"), InMemoryLedger(), at=WROTE)
    ledger = InMemoryLedger()
    write_shadow_row(ledger)
    with_row = run_tick(build_home(tmp_path / "b"), ledger, at=WROTE)
    before = next(p for p in without.pieces if p.piece == 1)
    after = next(p for p in with_row.pieces if p.piece == 1)
    assert (after.status, after.draw, after.reading_state) == (
        before.status,
        before.draw,
        before.reading_state,
    )
    assert before.shadows == ()
    assert len(after.shadows) == 1


def test_failing_control_a_drill_row_draws_no_shadow_line(tmp_path: Path) -> None:
    ledger = InMemoryLedger()
    write_shadow_row(ledger, fault=EnumGraphFault.DROP_CONTRACT_LINKS)
    floor = run_tick(build_home(tmp_path), ledger, at=WROTE + timedelta(minutes=5))
    assert piece_one(floor) == ()


def test_failing_control_an_unreadable_ledger_is_an_error_never_no_shadow_rows() -> (
    None
):
    ledger = InMemoryLedger()
    ledger.read_error = "ledger-down"
    read = read_ledger_facts(ledger, floor_overlay(), WROTE)
    assert read.facts is None
    assert read.error is not None


def test_a_row_with_an_unusable_cell_is_counted_and_not_shown() -> None:
    ledger = InMemoryLedger()
    write_shadow_row(ledger)
    ledger.lines[0] = ledger.lines[0].replace("max_age=3600", "max_age=never")
    read = read_ledger_facts(ledger, floor_overlay(), WROTE + timedelta(minutes=1))
    assert read.facts is not None
    assert read.facts.shadows == ()
    assert read.facts.shadows_rejected == 1


def test_the_simple_page_draws_each_shadow_line_with_its_age_and_that_it_holds_no_merge() -> (
    None
):
    page = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/simple.html"
    ).read_text(encoding="utf-8")
    assert "p.shadows" in page
    assert "ago(s.age_s)" in page
    assert "it holds no merge" in page
    assert "(stale)" in page


def test_a_failed_run_shows_its_outcome_not_a_fresh_empty_inventory(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger()
    result = CodeGraphShadowCheck(
        overlay=overlay(),
        host=HOST,
        source=memory_source({CLEAN_SHA: WIRED}),
        pull_requests=InMemoryPullRequestSource(error="state-gone"),
        appender=ledger,
        clock=lambda: WROTE,
    ).handle(ModelShadowCommand(correlation_id="floor-shadow-failed"))
    assert result.ledger == "written"
    floor = run_tick(build_home(tmp_path), ledger, at=WROTE + timedelta(minutes=5))
    (line,) = piece_one(floor)
    assert "source-unreadable" in line.text


def test_a_shadow_query_that_fails_keeps_the_go_and_the_other_facts() -> None:
    class _FailsOnShadowRead:
        def __init__(self, inner: InMemoryLedger, fail_call: int) -> None:
            self._inner = inner
            self._fail_call = fail_call
            self.calls = 0

        def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
            self.calls += 1
            if self.calls == self._fail_call:
                return ModelLedgerRead(error="shadow-read-down")
            return self._inner.read(query)

    inner = InMemoryLedger()
    write_shadow_row(inner)
    counting = _FailsOnShadowRead(inner, fail_call=0)
    read_ledger_facts(counting, floor_overlay(), WROTE + timedelta(minutes=1))
    last = counting.calls  # the shadow rules are read last
    failing = _FailsOnShadowRead(inner, fail_call=last)
    read = read_ledger_facts(failing, floor_overlay(), WROTE + timedelta(minutes=1))
    assert read.error is None
    assert read.facts is not None
    assert read.facts.shadows == ()
    assert read.facts.shadows_unread == (
        "STATUS rows of code-graph unread: shadow-read-down",
    )
