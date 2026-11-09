# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor reads the go from RULING rows by the pieces they name (OMN-23313).

The operator opened rebuild pieces 7 and 11 on 2026-11-09 (RULING 12:43:41Z, recorded with ``--amends`` of the 11-07 piece
ruling), but the floor's go stayed at piece 6: its go pattern needs ``start rebuild pieces N`` and an answer starting ``go``,
and this ruling asks ``Open rebuild piece 7 ... and piece 11 ...`` and is answered ``Ok agreed``. The rows below are those
rows as the ledger recorded them (the principal cell scrubbed). The failing control is the same rows read by a rule that
declares no open pattern, which is what the overlay was before this change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorSimple,
    ModelFloorSimpleOverlay,
    ModelFloorSimpleTickRequest,
    ModelGoRule,
)
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    go_given_through,
    opened_pieces,
    pieces_named,
    read_ledger_facts,
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
AT = datetime(2026, 11, 9, 13, 30, tzinfo=UTC)

# The piece 9 ruling of 2026-11-07 as recorded: it grants 9 and holds 7, in words that are not an open of 7.
PIECE_9_RULING = (
    "2026-11-07T12:19:45Z | RULING | lane=orchestrator-31d9 | principal=dev-recorded | ticket=OMN-17427 | "
    "question=Should rebuild piece 9 (landing and merge queue) get its go now, with piece 7 held until M0 PASS? | "
    "kind=decision | verbatim=encoded | "
    '"Operator, 2026-11-07 ~12:30Z, replying to the recommendation, verbatim: %22Yes. Go on 9.%22 Rebuild piece 9 has its go; '
    'piece 7 stays held until M0 PASS."'
)
# The ruling of 2026-11-09 as recorded: opens 7 and 11, pauses 10 and 12 to 16.
OPEN_7_AND_11 = (
    "2026-11-09T12:43:41Z | RULING | lane=orchestrator-31d9 | principal=dev-recorded | ticket=OMN-17427 | "
    "question=Open rebuild piece 7 (process spine) and piece 11 (dev deploy and runtime) now beside the open joins, each live "
    "only once shown working and measured 7 days against the old way, with pieces 10 and 12 to 16 paused until the joins "
    "below pass? | kind=decision | amends=2026-11-07T12:19:45Z | "
    '"Ok agreed"'
)
HOLD_7 = (
    "2026-11-09T15:00:00Z | RULING | lane=orchestrator-31d9 | principal=dev-recorded | ticket=OMN-17427 | "
    "question=Open rebuild piece 7 (process spine) now? | kind=decision | "
    '"No. Hold piece 7."'
)


def overlay() -> ModelFloorSimpleOverlay:
    return ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )


def lines(*extra: str) -> list[str]:
    base = [x for x in FIXTURE.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [*base, *extra]


def rows(source: list[str]) -> list[ModelLedgerEvidenceRow]:
    parsed = [evidence_row_from_text(x) for x in source]
    return [r for r in parsed if r is not None]


def without_open_rule() -> ModelGoRule:
    """The go rule as the overlay declared it before this change: no open pattern."""
    return overlay().rebuild.go.model_copy(
        update={
            "open_tickets": (),
            "open_pattern": None,
            "open_affirmative_pattern": None,
        }
    )


def run_tick(home: Path, ledger: InMemoryLedger) -> ModelFloorSimple:
    handler = HandlerFloorSimpleTick(
        sink=InMemoryEmissionSink(),
        clock=lambda: AT,
        served_alarm_ids=lambda: None,
        ledger_source=ledger,
    )
    result = handler.run(
        ModelFloorSimpleTickRequest(home=str(home), now=AT), environ=environ(home)
    )
    assert result.ok
    return ModelFloorSimple.model_validate_json(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text(
            encoding="utf-8"
        )
    )


# --- the subject of a ruling ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7 (process spine) and piece 11", (7, 11)),
        ("2 to 6", (2, 3, 4, 5, 6)),
        ("3, 5 and 9 to 10", (3, 5, 9, 10)),
        ("6 to 4", (6,)),
    ],
)
def test_pieces_named_reads_numbers_ranges_and_skips_labels(
    text: str, expected: tuple[int, ...]
) -> None:
    assert pieces_named(text) == expected


# --- the go by subject ----------------------------------------------------------------------------------------------


def test_go_by_subject_the_ruling_of_11_09_opens_pieces_7_and_11_and_no_other() -> None:
    rule = overlay().rebuild.go
    given = rows(lines(PIECE_9_RULING, OPEN_7_AND_11))
    # The in-order go stays where the recorded start rulings put it; 7 and 11 are opened by name, beside it.
    assert go_given_through(given, rule) == 6
    assert opened_pieces(given, rule) == (7, 11)


def test_go_by_subject_the_ruling_that_holds_piece_7_opens_nothing() -> None:
    rule = overlay().rebuild.go
    assert opened_pieces(rows(lines(PIECE_9_RULING)), rule) == ()


def test_go_by_subject_a_later_answer_that_is_not_a_yes_takes_the_piece_back() -> None:
    rule = overlay().rebuild.go
    assert opened_pieces(rows(lines(OPEN_7_AND_11, HOLD_7)), rule) == (11,)


def test_go_by_subject_failing_control_the_old_rule_opens_neither_piece() -> None:
    """Without the open pattern (the overlay before this change) the same rows open nothing: the floor stays at 6."""
    old = without_open_rule()
    given = rows(lines(PIECE_9_RULING, OPEN_7_AND_11))
    assert go_given_through(given, old) == 6
    assert opened_pieces(given, old) == ()


def test_go_by_subject_a_rule_with_half_an_open_declaration_is_refused() -> None:
    with pytest.raises(ValueError, match="declared together"):
        ModelGoRule.model_validate(
            {
                **overlay().rebuild.go.model_dump(),
                "open_affirmative_pattern": None,
            }
        )


def test_go_by_subject_the_facts_carry_the_opened_pieces_and_the_opening_row() -> None:
    facts = read_ledger_facts(InMemoryLedger(lines(OPEN_7_AND_11)), overlay(), AT).facts
    assert facts is not None
    assert facts.go_pieces == (7, 11)
    assert facts.go_rows[-1] == "2026-11-09T12:43:41Z"


# --- the floor ------------------------------------------------------------------------------------------------------


def test_go_by_subject_the_floor_admits_pieces_7_and_11_and_keeps_10_and_12_waiting(
    tmp_path: Path,
) -> None:
    doc = run_tick(build_home(tmp_path), InMemoryLedger(lines(OPEN_7_AND_11)))
    by_piece = {p.piece: p for p in doc.pieces}
    assert doc.rebuild.go_given_through == 6
    assert doc.rebuild.go_pieces == (7, 11)
    for n in (7, 11):
        assert by_piece[n].status != "not_started"
        assert by_piece[n].text.startswith("admitted")
    for n in (10, 12, 13, 14, 15, 16):
        assert by_piece[n].status == "not_started"


def test_go_by_subject_the_floor_without_the_ruling_row_leaves_7_and_11_not_started(
    tmp_path: Path,
) -> None:
    """The failing control on the floor: the rows of before the ruling leave both pieces where the served floor had them."""
    doc = run_tick(build_home(tmp_path), InMemoryLedger(lines(PIECE_9_RULING)))
    by_piece = {p.piece: p for p in doc.pieces}
    assert doc.rebuild.go_pieces == ()
    assert by_piece[7].status == "not_started"
    assert by_piece[11].status == "not_started"
