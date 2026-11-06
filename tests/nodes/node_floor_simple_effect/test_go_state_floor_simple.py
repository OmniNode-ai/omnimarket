# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor reads the go and the start of piece 1 from the ledger (OMN-22674).

The served floor said "Waiting on your go", go 0 and piece 1 not started at 15:00Z on 2026-11-06, after the operator had
said go to piece 1 (RULING 12:10:28Z) and to pieces 2 to 6 (RULING 13:16:32Z), and while the code graph wrote its golden
and error rows. The rows in the fixture file are those rows as the ledger recorded them (the principal cell scrubbed).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorSimple,
    ModelFloorSimpleOverlay,
    ModelFloorSimpleTickRequest,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.emission_sink import (
    InMemoryEmissionSink,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    go_given_through,
    piece_events,
    read_ledger_facts,
    verbatim_answer,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from tests.nodes.node_floor_simple_effect.support import (
    build_home,
    environ,
    overlay_text,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "ledger_rows_go_and_code_graph.txt"
AT = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)
FIRST_GO = "2026-11-06T12:10:28Z"
PIECES_2_TO_6_GO = "2026-11-06T13:16:32Z"


def overlay() -> ModelFloorSimpleOverlay:
    return ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )


def lines() -> list[str]:
    return [x for x in FIXTURE.read_text(encoding="utf-8").splitlines() if x.strip()]


def lines_without(*stamps: str) -> list[str]:
    return [x for x in lines() if not any(x.startswith(s) for s in stamps)]


def rows(source: list[str]) -> list:  # type: ignore[type-arg]
    parsed = [evidence_row_from_text(x) for x in source]
    return [r for r in parsed if r is not None]


def run_tick(
    home: Path, ledger: InMemoryLedger
) -> tuple[ModelFloorSimple, InMemoryEmissionSink]:
    sink = InMemoryEmissionSink()
    handler = HandlerFloorSimpleTick(
        sink=sink,
        clock=lambda: AT,
        served_alarm_ids=lambda: None,
        ledger_source=ledger,
    )
    result = handler.run(
        ModelFloorSimpleTickRequest(home=str(home), now=AT), environ=environ(home)
    )
    assert result.ok
    doc = ModelFloorSimple.model_validate_json(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text(
            encoding="utf-8"
        )
    )
    return doc, sink


# --- the go ---------------------------------------------------------------------------------------------------------


def test_go_state_the_two_recorded_rulings_give_the_go_through_piece_6() -> None:
    rule = overlay().rebuild.go
    assert go_given_through(rows(lines()), rule) == 6
    # The words are read as the operator said them, quotes off.
    answers = {
        r.stamp.strftime("%Y-%m-%dT%H:%M:%SZ"): verbatim_answer(r)
        for r in rows(lines())
        if r.row_type == "RULING"
    }
    assert answers[FIRST_GO] == "go. ."
    assert answers[PIECES_2_TO_6_GO] == "You may start rebuild pieces 2 to 6."


def test_go_state_the_first_ruling_alone_covers_piece_1_and_the_second_alone_leaves_a_gap() -> (
    None
):
    rule = overlay().rebuild.go
    assert go_given_through(rows(lines_without(PIECES_2_TO_6_GO)), rule) == 1
    # Pieces are taken in order: a go for 2 to 6 with no go for piece 1 reaches nothing.
    assert go_given_through(rows(lines_without(FIRST_GO)), rule) == 0


def test_go_state_a_ruling_about_something_else_moves_nothing() -> None:
    rule = overlay().rebuild.go
    only_unrelated = lines_without(FIRST_GO, PIECES_2_TO_6_GO)
    assert any("one container plan" in x for x in only_unrelated)
    assert go_given_through(rows(only_unrelated), rule) == 0


def test_go_state_a_later_answer_that_is_not_a_yes_takes_the_go_back() -> None:
    rule = overlay().rebuild.go
    taken_back = (
        "2026-11-06T16:00:00Z | RULING | lane=orchestrator-31d9 | principal=dev-recorded | ticket=OMN-22478 | "
        "question=Start rebuild pieces 4 to 6 now? | kind=decision | "
        '"Not yet. Hold pieces 4 to 6."'
    )
    assert go_given_through(rows([*lines(), taken_back]), rule) == 3


def test_go_state_the_old_reader_gave_zero_and_not_started_for_the_same_day(
    tmp_path: Path,
) -> None:
    """The failing control: with no go rows read (what the hand-set value amounted to) the floor says what it said at 15:00Z."""
    doc, _ = run_tick(build_home(tmp_path), InMemoryLedger([]))
    assert doc.rebuild.go_given_through == 0
    assert doc.rebuild.headline == "Piece 1, code graph: not started."
    assert doc.rebuild.next_text.startswith("Waiting on your go.")
    assert doc.pieces[0].status == "not_started"


def test_go_state_the_floor_with_the_ledger_rows_has_the_go_and_no_wait(
    tmp_path: Path,
) -> None:
    doc, _ = run_tick(build_home(tmp_path), InMemoryLedger(lines()))
    assert doc.rebuild.go_given_through == 6
    assert "Waiting on your go" not in doc.rebuild.next_text
    assert doc.sources_read["ledger"] is True
    by_piece = {p.piece: p for p in doc.pieces}
    # Pieces 2 to 6 have the go and no events yet: admitted, never "not started" and never waiting on a go.
    for n in range(2, 7):
        assert by_piece[n].status == "unknown"
        assert by_piece[n].text.startswith("admitted")
    assert by_piece[7].status == "not_started"


# --- the events of piece 1 ------------------------------------------------------------------------------------------


def test_piece_events_the_code_graph_rows_are_one_golden_chain_and_one_error_chain() -> (
    None
):
    events = piece_events(rows(lines()), overlay().piece_events)
    golden = [e for e in events if e.phase == "success"]
    failed = [e for e in events if e.phase == "failure"]
    # Two golden runs (13:21:18Z and 13:49:22Z). The drill row that still read WORKING (13:24:38Z) is not a golden run.
    assert sorted(e.emitted_at.strftime("%H:%M:%S") for e in golden) == [
        "13:21:18",
        "13:49:22",
    ]
    assert [
        (e.emitted_at.strftime("%H:%M:%S"), e.failure_code, e.drill) for e in failed
    ] == [("13:24:31", "unreadable-repo", True)]
    assert {e.piece for e in events} == {1}
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=AT,
            emissions=events,
            pieces=overlay().pieces,
            joins=overlay().joins,
            freshness_max_age_s=overlay().freshness_max_age_s,
            error_max_age_s=overlay().error_receipt_max_age_s,
            chain_deadline_s=overlay().chain_deadline_s,
        )
    )
    piece1 = receipts.pieces[0]
    assert piece1.started is True
    assert piece1.golden_at == datetime(2026, 11, 6, 13, 49, 22, tzinfo=UTC)
    assert piece1.error_at == datetime(2026, 11, 6, 13, 24, 31, tzinfo=UTC)
    assert receipts.findings == ()


def test_piece_events_piece_1_reads_started_on_the_floor_with_its_golden_and_error_ages(
    tmp_path: Path,
) -> None:
    doc, _ = run_tick(build_home(tmp_path), InMemoryLedger(lines()))
    piece1 = doc.pieces[0]
    assert piece1.status == "started"
    assert piece1.draw == "green"
    assert piece1.text.startswith("started; no reading row yet")
    assert piece1.golden_age_s == 70 * 60 + 38
    assert piece1.error_age_s == 95 * 60 + 29
    assert doc.rebuild.headline == "Piece 1, code graph: started."
    assert doc.rebuild.next_text == "Next is hosts and capacity."


def test_piece_events_with_only_the_golden_row_piece_1_has_not_started(
    tmp_path: Path,
) -> None:
    without_error = lines_without("2026-11-06T13:24:31Z")
    doc, _ = run_tick(build_home(tmp_path), InMemoryLedger(without_error))
    # The go is read, the golden event is there and the error event is not: a piece starts on both.
    assert doc.pieces[0].status == "unknown"
    assert doc.pieces[0].text.startswith("admitted")


# --- a ledger that cannot be read -----------------------------------------------------------------------------------


def test_ledger_unreadable_the_go_and_the_events_are_drawn_unread_never_zero(
    tmp_path: Path,
) -> None:
    broken = InMemoryLedger(lines())
    broken.read_error = "ledger-file-unreadable:PermissionError"
    doc, _ = run_tick(build_home(tmp_path), broken)
    assert doc.rebuild.go_given_through is None
    assert doc.rebuild.next_text.startswith("The go could not be read from the ledger.")
    assert "Waiting on your go" not in doc.rebuild.next_text
    assert doc.sources_read["ledger"] is False
    by_piece = {p.piece: p for p in doc.pieces}
    for n in (1, 6, 18):
        assert by_piece[n].status == "unknown"
        assert by_piece[n].cause_id == "source:ledger"
    assert not any(p.status == "not_started" for p in doc.pieces)
    assert any(c.id == "source:ledger" for c in doc.causes)


def test_ledger_unreadable_the_read_names_the_reason_and_is_not_an_empty_answer() -> (
    None
):
    broken = InMemoryLedger(lines())
    broken.read_error = "read-command-exit-1"
    result = read_ledger_facts(broken, overlay(), AT)
    assert result.facts is None
    assert result.error == "go rulings unread: read-command-exit-1"


def test_ledger_unreadable_a_route_the_environment_does_not_name_is_unread(
    tmp_path: Path,
) -> None:
    handler = HandlerFloorSimpleTick(
        sink=InMemoryEmissionSink(), clock=lambda: AT, served_alarm_ids=lambda: None
    )
    home = build_home(tmp_path)
    result = handler.run(
        ModelFloorSimpleTickRequest(home=str(home), now=AT), environ=environ(home)
    )
    assert result.ok
    doc = ModelFloorSimple.model_validate_json(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text(
            encoding="utf-8"
        )
    )
    assert doc.rebuild.go_given_through is None
    assert doc.sources_read["ledger"] is False
