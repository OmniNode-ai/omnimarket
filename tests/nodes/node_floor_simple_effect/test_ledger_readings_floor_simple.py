# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor draws each piece's reading from the reading writer's rows on the ledger (OMN-22478).

At 17:25Z on 2026-11-06 floor-simple.json carried ``reading_state: null`` for every piece, including piece 6, which the
reading writer had written WORKING in its 16:00Z window, and piece 4, written NOT_WORKING in the same run. The floor read a
readings file that nothing writes; the writer appends its rows to the ledger. The rows in the fixture file are the writer's
rows of two windows as the ledger recorded them (the principal cell scrubbed).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorSimple,
    ModelFloorSimpleOverlay,
    ModelFloorSimpleTickRequest,
)
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_floor_simple_effect.handlers.emission_sink import (
    InMemoryEmissionSink,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    read_ledger_facts,
    reading_rows,
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
READINGS = FIXTURES / "ledger_rows_reading_writer.txt"
AT = datetime(2026, 11, 6, 19, 0, tzinfo=UTC)
LATE = datetime(2026, 11, 6, 22, 0, tzinfo=UTC)  # past the newest window's 7200 s bound


def overlay() -> ModelFloorSimpleOverlay:
    return ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )


def lines(path: Path) -> list[str]:
    return [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def rows(source: list[str]) -> list[ModelLedgerEvidenceRow]:
    parsed = [evidence_row_from_text(x) for x in source]
    return [r for r in parsed if r is not None]


def run_tick(
    home: Path,
    ledger: InMemoryLedger,
    *,
    at: datetime = AT,
    roots: Path = OVERLAYS,
) -> ModelFloorSimple:
    env = {**environ(home), "ONEX_SKILL_OVERLAY_ROOTS": str(roots)}
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


def without_readings_rule(tmp_path: Path) -> Path:
    """An overlay root whose floor overlay has no ledger_readings block: what the floor was before this change."""
    root = tmp_path / "overlays-old"
    source = OVERLAYS / "node_floor_simple_compute" / "overlay.yaml"
    target = root / "node_floor_simple_compute" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    kept: list[str] = []
    skipping = False
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.startswith("ledger_readings:"):
            skipping = True
            continue
        if skipping and (line.startswith(" ") or not line.strip()):
            continue
        skipping = False
        kept.append(line)
    target.write_text("\n".join(kept) + "\n", encoding="utf-8")
    for other in OVERLAYS.iterdir():
        if other.name != "node_floor_simple_compute":
            (root / other.name).symlink_to(other, target_is_directory=True)
    return root


def test_the_reading_rows_give_the_newest_reading_of_each_piece() -> None:
    rule = overlay().ledger_readings
    assert rule is not None
    readings, rejected = reading_rows(rows(lines(READINGS)), rule)
    by_piece = {r.piece: r for r in readings}
    assert sorted(by_piece) == list(range(1, 19))
    assert rejected == 0
    # Newest window wins: piece 3 read UNKNOWN at 17:15Z and WORKING at 18:55Z, piece 5 UNKNOWN then NOT_WORKING.
    assert by_piece[3].state == "WORKING"
    assert by_piece[5].state == "NOT WORKING"
    assert by_piece[4].state == "NOT WORKING"
    assert by_piece[6].state == "WORKING"
    assert by_piece[2].state == "UNKNOWN"
    assert by_piece[3].published_at == datetime(2026, 11, 6, 18, 55, 37, tzinfo=UTC)
    assert by_piece[3].max_age_s == 7200


def test_a_row_that_is_not_a_reading_is_counted_and_never_drawn() -> None:
    rule = overlay().ledger_readings
    assert rule is not None
    good = lines(READINGS)[0]
    broken = [
        good.replace("verdict=WORKING", "verdict=FINE"),
        good.replace("piece=1 ", "piece=99 "),
        good.replace("max_age=93600", "max_age=0").replace("max_age=7200", "max_age=0"),
        good.replace("probed_at=", "probed_on="),
    ]
    readings, rejected = reading_rows(rows(broken), rule)
    assert readings == ()
    assert rejected == 4


def test_a_row_of_another_lane_or_source_is_not_a_reading_row() -> None:
    rule = overlay().ledger_readings
    assert rule is not None
    other_lane = lines(READINGS)[0].replace("lane=reading-writer", "lane=host-reading")
    other_src = lines(READINGS)[0].replace("src=reading", "src=probe")
    assert reading_rows(rows([other_lane, other_src]), rule) == ((), 0)


def test_the_floor_draws_every_piece_reading_from_the_ledger(tmp_path: Path) -> None:
    ledger = InMemoryLedger([*lines(GO_RULINGS), *lines(READINGS)])
    doc = run_tick(build_home(tmp_path), ledger)
    by_piece = {p.piece: p for p in doc.pieces}
    assert by_piece[6].reading_state == "WORKING"
    assert by_piece[3].reading_state == "WORKING"
    assert by_piece[4].reading_state == "NOT WORKING"
    assert by_piece[5].reading_state == "NOT WORKING"
    assert by_piece[2].reading_state == "UNKNOWN"
    assert by_piece[6].reading_age_s == 4 * 60 + 23  # probed 18:55:37Z, read at 19:00Z
    assert by_piece[6].reading_max_age_s == 7200
    assert by_piece[4].status == "not_working"
    assert doc.sources_read["ledger"] is True


def test_the_failing_control_without_the_rule_every_reading_is_null_as_it_was(
    tmp_path: Path,
) -> None:
    """The floor of 17:25Z: the same ledger rows, the overlay of before this change, no piece has a reading."""
    ledger = InMemoryLedger([*lines(GO_RULINGS), *lines(READINGS)])
    doc = run_tick(build_home(tmp_path), ledger, roots=without_readings_rule(tmp_path))
    by_piece = {p.piece: p for p in doc.pieces}
    assert by_piece[6].reading_state is None
    assert by_piece[4].reading_state is None
    assert by_piece[6].reading_age_s is None


def test_a_reading_older_than_its_bound_is_not_working_and_says_it_is_stale(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger([*lines(GO_RULINGS), *lines(READINGS)])
    doc = run_tick(build_home(tmp_path), ledger, at=LATE)
    by_piece = {p.piece: p for p in doc.pieces}
    assert by_piece[6].reading_state == "NOT WORKING"
    assert "stale, read" in by_piece[6].text


def test_a_ledger_that_cannot_be_read_draws_no_reading_and_says_the_ledger_is_unread(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger([*lines(GO_RULINGS), *lines(READINGS)])
    ledger.read_error = "route refused"
    doc = run_tick(build_home(tmp_path), ledger)
    by_piece = {p.piece: p for p in doc.pieces}
    assert by_piece[6].reading_state is None
    assert doc.sources_read["ledger"] is False
    failed = read_ledger_facts(ledger, overlay(), AT)
    assert failed.facts is None
    assert failed.error is not None
    assert "unread: route refused" in failed.error


def test_the_facts_carry_the_readings_and_the_count_of_rows_that_were_not_readings() -> (
    None
):
    broken = lines(READINGS)[0].replace("piece=1 ", "piece=0 ")
    facts = read_ledger_facts(
        InMemoryLedger([*lines(GO_RULINGS), *lines(READINGS), broken]), overlay(), AT
    ).facts
    assert facts is not None
    assert [r.piece for r in facts.readings] == list(range(1, 19))
    assert facts.readings_rejected == 1


def test_a_piece_the_go_does_not_cover_and_that_reads_unknown_is_not_started_and_no_cause(
    tmp_path: Path,
) -> None:
    """At 20:24Z on 2026-11-06 the floor went from 30 to 46 red: pieces 7 to 18 each drew "is not working, reading UNKNOWN".

    The go runs through piece 6. A piece beyond it whose reading is UNKNOWN (the writer has no producer for it) is not a
    failure: it draws as not started and raises no cause. Only admitted pieces count.
    """
    ledger = InMemoryLedger([*lines(GO_RULINGS), *lines(READINGS)])
    doc = run_tick(build_home(tmp_path), ledger)
    assert doc.rebuild.go_given_through == 6
    by_piece = {p.piece: p for p in doc.pieces}
    unadmitted_unknown = [
        n for n in range(7, 19) if by_piece[n].reading_state == "UNKNOWN" and n != 17
    ]
    assert len(unadmitted_unknown) >= 8
    for n in unadmitted_unknown:
        assert by_piece[n].status == "not_started", n
        assert by_piece[n].draw == "grey", n
    caused = {c.id for c in doc.causes}
    assert not {f"piece:{n}" for n in unadmitted_unknown} & caused


def test_a_piece_beyond_the_go_with_a_real_reading_still_draws_it(
    tmp_path: Path,
) -> None:
    """Piece 15 reads WORKING in the recorded rows although the go stops at 6: a real reading is drawn as read."""
    ledger = InMemoryLedger([*lines(GO_RULINGS), *lines(READINGS)])
    doc = run_tick(build_home(tmp_path), ledger)
    by_piece = {p.piece: p for p in doc.pieces}
    assert by_piece[15].reading_state == "WORKING"
    assert by_piece[15].status == "working"


def test_an_admitted_piece_that_reads_unknown_is_still_a_cause(tmp_path: Path) -> None:
    ledger = InMemoryLedger([*lines(GO_RULINGS), *lines(READINGS)])
    doc = run_tick(build_home(tmp_path), ledger)
    by_piece = {p.piece: p for p in doc.pieces}
    assert by_piece[2].reading_state == "UNKNOWN"
    assert by_piece[2].status == "unknown"
    assert "piece:2" in {c.id for c in doc.causes}
