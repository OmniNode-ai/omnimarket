# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pieces 2 to 6 draw their golden and error events from the rows they write on the ledger (OMN-22739).

The floor overlay listed piece_events for piece 1 only, so pieces 2 to 6 read "admitted; no golden and error event yet" at
17:10Z on 2026-11-06 although each wrote ledger rows. The rows in the fixture file are those rows as the ledger recorded
them (the principal cell scrubbed), with two rows of other probes of the same lane as controls that are no piece's event.
A golden row is a WORKING row that is not a drill; an error row is any NOT_WORKING row, so a real failure counts as a typed
failure and a drill stays marked as one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorSimple,
    ModelFloorSimpleOverlay,
    ModelFloorSimpleTickRequest,
    ModelNodeEmission,
)
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_floor_simple_effect.handlers.emission_sink import (
    InMemoryEmissionSink,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from tests.nodes.node_floor_simple_effect.support import (
    OVERLAYS,
    build_home,
    environ,
    overlay_text,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
GO_RULINGS = FIXTURES / "ledger_rows_go_and_code_graph.txt"
PIECE_ROWS = FIXTURES / "ledger_rows_piece_events.txt"
AT = datetime(2026, 11, 6, 19, 10, tzinfo=UTC)
PIECES = (2, 3, 4, 5, 6)


def overlay() -> ModelFloorSimpleOverlay:
    return ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )


def lines(path: Path) -> list[str]:
    return [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def rows() -> list[ModelLedgerEvidenceRow]:
    parsed = [evidence_row_from_text(x) for x in lines(PIECE_ROWS)]
    return [r for r in parsed if r is not None]


def events_of(piece: int) -> list[ModelNodeEmission]:
    return [e for e in piece_events(rows(), overlay().piece_events) if e.piece == piece]


def starts_of(piece: int) -> int:
    return len([e for e in events_of(piece) if e.phase == "start"])


def lines_where(lane: str, *needles: str, absent: tuple[str, ...] = ()) -> int:
    return len(
        [
            x
            for x in lines(PIECE_ROWS)
            if f"lane={lane} " in x
            and all(n in x for n in needles)
            and not any(a in x for a in absent)
        ]
    )


def run_tick(home: Path, ledger: InMemoryLedger, roots: Path) -> ModelFloorSimple:
    env = {**environ(home), "ONEX_SKILL_OVERLAY_ROOTS": str(roots)}
    handler = HandlerFloorSimpleTick(
        sink=InMemoryEmissionSink(),
        clock=lambda: AT,
        served_alarm_ids=lambda: None,
        ledger_source=ledger,
    )
    result = handler.run(
        ModelFloorSimpleTickRequest(home=str(home), now=AT), environ=env
    )
    assert result.ok
    return ModelFloorSimple.model_validate_json(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text(
            encoding="utf-8"
        )
    )


def overlay_without_piece_rules_2_to_6(tmp_path: Path) -> Path:
    """The overlay of before this change: piece 1 is the only piece with event rules."""
    root = tmp_path / "overlays-old"
    source = OVERLAYS / "node_floor_simple_compute" / "overlay.yaml"
    target = root / "node_floor_simple_compute" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    text = source.read_text(encoding="utf-8")
    head, _, tail = text.partition("  - piece: 2\n    node:")
    assert tail, "the overlay carries rules for pieces 2 to 6"
    target.write_text(head, encoding="utf-8")
    for other in OVERLAYS.iterdir():
        if other.name != "node_floor_simple_compute":
            (root / other.name).symlink_to(other, target_is_directory=True)
    return root


def test_every_piece_from_2_to_6_has_a_rule_and_each_rule_reads_only_its_own_rows() -> (
    None
):
    rules = {r.piece: r for r in overlay().piece_events}
    assert sorted(rules) == [1, *PIECES]
    # The two probes of the host-reading lane that are no piece's event name no rule.
    assert lines_where("host-reading", "probe=ledger-old-names") == 1
    assert lines_where("host-reading", "probe=ledger-outbox") == 1
    other_probe_rows = [
        r
        for r in rows()
        if r.cells.get("probe") in ("ledger-old-names", "ledger-outbox")
    ]
    assert len(other_probe_rows) == 2
    assert piece_events(other_probe_rows, overlay().piece_events) == ()


def test_piece_3_a_working_row_that_is_no_drill_is_golden_and_a_drill_is_a_marked_failure() -> (
    None
):
    events = events_of(3)
    working = lines_where("seam-deadline", "verdict=WORKING", "drill=no")
    failed = lines_where("seam-deadline", "verdict=NOT_WORKING")
    assert working >= 1
    assert failed >= 1
    assert len([e for e in events if e.phase == "success"]) == working
    failures = [e for e in events if e.phase == "failure"]
    assert len(failures) == failed
    assert all(
        e.drill for e in failures
    )  # every recorded failure of this lane is a drill
    assert all(e.failure_code for e in failures)


def test_piece_6_the_durability_probe_is_golden_when_working_and_a_failure_when_not() -> (
    None
):
    events = events_of(6)
    working = lines_where(
        "host-reading", "probe=ledger-durability", "verdict=WORKING", "drill=no"
    )
    failed = lines_where(
        "host-reading", "probe=ledger-durability", "verdict=NOT_WORKING"
    )
    assert working >= 1
    assert failed >= 1
    assert len([e for e in events if e.phase == "success"]) == working
    assert len([e for e in events if e.phase == "failure"]) == failed
    assert any(e.failure_code == "no-durable-row-by-deadline" for e in events)


def test_piece_2_the_canary_that_never_read_working_counts_every_failure_real_or_drill() -> (
    None
):
    events = events_of(2)
    failed = lines_where("host-reading", "probe=host-canary", "verdict=NOT_WORKING")
    assert failed >= 4
    assert [e for e in events if e.phase == "success"] == []
    failures = [e for e in events if e.phase == "failure"]
    assert len(failures) == failed
    # A real failure (drill=no, or no drill cell) is a failure and is not a drill; the drill row stays one.
    assert len([e for e in failures if e.drill]) == lines_where(
        "host-reading", "probe=host-canary", "drill=yes"
    )
    assert len([e for e in failures if not e.drill]) >= 3
    assert any("canary-unreachable-h106" in e.failure_code for e in failures)


def test_piece_4_and_piece_5_count_their_real_not_working_rows_and_have_no_golden_yet() -> (
    None
):
    for piece, lane in ((4, "overlay-drift"), (5, "secret-reading")):
        events = events_of(piece)
        failed = lines_where(lane, "verdict=NOT_WORKING")
        assert failed >= 1
        assert len([e for e in events if e.phase == "failure"]) == failed
        golden = lines_where(lane, "verdict=WORKING", "drill=no")
        assert len([e for e in events if e.phase == "success"]) == golden
    # Piece 4 has no WORKING row and no drill cell, piece 5's drill row is marked a drill.
    assert [e for e in events_of(4) if e.phase == "success"] == []
    assert not any(e.drill for e in events_of(4))
    assert any(e.drill for e in events_of(5))
    assert starts_of(4) == len([e for e in events_of(4) if e.phase == "failure"])


def test_the_floor_draws_events_for_pieces_2_to_6_from_their_rows(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger([*lines(GO_RULINGS), *lines(PIECE_ROWS)])
    doc = run_tick(build_home(tmp_path), ledger, OVERLAYS)
    by_piece = {p.piece: p for p in doc.pieces}
    for piece in PIECES:
        assert "no events yet" not in by_piece[piece].text, piece
        assert by_piece[piece].error_age_s is not None, piece
    # Pieces 3 and 6 read WORKING, so they have a golden event and are started.
    for piece in (3, 6):
        assert by_piece[piece].status == "started", piece
        assert by_piece[piece].golden_age_s is not None, piece
    # Pieces 2, 4 and 5 never read WORKING: the floor says "golden never" and does not invent a golden event.
    for piece in (2, 4, 5):
        assert by_piece[piece].golden_age_s is None, piece
        assert "golden never, error" in by_piece[piece].text, piece
    # A real failure of piece 2 that piece 3 did not answer with a typed failure is a broken chain the floor now shows.
    assert any(f.subject.startswith("J2 ") for f in doc.chain_findings)


def test_the_failing_control_without_the_rules_pieces_2_to_6_draw_no_events(
    tmp_path: Path,
) -> None:
    """The floor of 17:10Z: the same ledger rows, the overlay of before, every piece 'admitted; no events yet'."""
    ledger = InMemoryLedger([*lines(GO_RULINGS), *lines(PIECE_ROWS)])
    doc = run_tick(
        build_home(tmp_path), ledger, overlay_without_piece_rules_2_to_6(tmp_path)
    )
    by_piece = {p.piece: p for p in doc.pieces}
    for piece in PIECES:
        assert by_piece[piece].status == "unknown", piece
        assert by_piece[piece].text.startswith("admitted"), piece
        assert by_piece[piece].golden_age_s is None
        assert by_piece[piece].error_age_s is None
    assert not any(f.subject.startswith("J2 ") for f in doc.chain_findings)
