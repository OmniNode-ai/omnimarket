# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""READY means integrated (OMN-23081; operator ruling 2026-11-08 05:08:55Z).

On 2026-11-08 the board drew pieces 1, 2, 3, 5, 6, 8 and 15 as working while every join between them read broken or never run:
working but unintegrated looked done. Each control below failed on that board and passes only on the rule:

- a piece with a working reading and one broken join never reads READY;
- a broken or never-run join between pieces in scope is a red row, under the colour rule, with an owner ticket;
- READY appears only when every join that counts for the piece passes;
- the scope and the requirement are overlay data: change the overlay and the board changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.models.floor_17a import (
    ModelChainFinding,
    ModelFloorLane,
    ModelFloorReceipts,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
    ModelJoinReceipt,
    ModelPieceReceipt,
    ModelReadingRow,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
    check_red_rule,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 8, 5, 30, tzinfo=UTC)
# The pieces the overlay puts in scope, and the joins between two of them (the seven the ruling named owners for).
SCOPE = (1, 2, 3, 4, 5, 6, 8, 9, 17)
COUNTED = ("J0", "J1", "J2", "J3", "J4", "J5", "J8")
OWNERS = {
    "J0": "OMN-23022",
    "J1": "OMN-23082",
    "J2": "OMN-23083",
    "J3": "OMN-23084",
    "J4": "OMN-23085",
    "J5": "OMN-23086",
    "J8": "OMN-23087",
}


def overlay(**scope: Any) -> ModelFloorSimpleOverlay:
    raw = overlay_text("node_floor_simple_compute")
    raw["readiness"].update(scope)
    return ModelFloorSimpleOverlay.model_validate(raw)


def receipts(
    ov: ModelFloorSimpleOverlay,
    states: dict[str, str] | None = None,
    findings: tuple[ModelChainFinding, ...] = (),
) -> ModelFloorReceipts:
    """Every counted join passed unless ``states`` says otherwise; every join outside the scope never ran."""
    states = states or {}
    scope = set(ov.readiness.in_scope_pieces)
    joins = []
    for j in ov.joins:
        counted = j.from_piece in scope and j.to_piece in scope
        state = states.get(j.id, "passed" if counted else "not_run")
        ran = state != "not_run"
        joins.append(
            ModelJoinReceipt.model_validate(
                {
                    "join": j.id,
                    "from_piece": j.from_piece,
                    "to_piece": j.to_piece,
                    "golden_at": NOW - timedelta(minutes=10) if ran else None,
                    "error_at": NOW - timedelta(minutes=20) if ran else None,
                    "state": state,
                    # Real work crossed every join that ran (OMN-23862); the tests of that rule are in test_real_work_omn23862.
                    "real_golden_at": NOW - timedelta(minutes=10) if ran else None,
                }
            )
        )
    # Every piece has run its golden and its error chain, so a piece with a WORKING reading reads working and not "admitted".
    pieces = tuple(
        ModelPieceReceipt.model_validate(
            {
                "piece": p.piece,
                "started": True,
                "golden_at": NOW - timedelta(minutes=10),
                "error_at": NOW - timedelta(minutes=20),
                "golden_backends": ("real",),
                "last_emission_at": NOW - timedelta(minutes=10),
                "golden_age_s": 600,
                "error_age_s": 1200,
                "freshness": "fresh",
            }
        )
        for p in ov.pieces
    )
    return ModelFloorReceipts.model_validate(
        {
            "computed_at": NOW,
            "pieces": pieces,
            "joins": tuple(joins),
            "chains": (),
            "findings": findings,
        }
    )


def board(
    states: dict[str, str] | None = None,
    *,
    ov: ModelFloorSimpleOverlay | None = None,
    working: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 8, 9, 17, 15),
    lanes: tuple[ModelFloorLane, ...] = (),
    findings: tuple[ModelChainFinding, ...] = (),
) -> ModelFloorSimple:
    ov = ov or overlay()
    readings = {
        p: ModelReadingRow(
            piece=p,
            state="WORKING",
            probed_at=NOW - timedelta(minutes=5),
            published_at=NOW - timedelta(minutes=5),
            max_age_s=86400,
        )
        for p in working
    }
    return HandlerFloorSimple().handle(
        ModelFloorSimpleInput(
            now=NOW,
            overlay=ov,
            open_prs=0,
            open_prs_before=0,
            merges_in_window=0,
            watcher_read_at=NOW,
            slots_working=1,
            slots_total=2,
            record_read_at=NOW,
            lanes=lanes,
            lab=None,
            receipts=receipts(ov, states, findings),
            readings=readings,
            go_given_through=18,
        )
    )


def piece(doc: ModelFloorSimple, n: int):
    return next(p for p in doc.pieces if p.piece == n)


def join_cause(doc: ModelFloorSimple, join: str):
    return next((c for c in doc.causes if c.id == f"join:{join}"), None)


# --- a working piece with a broken join is never READY --------------------------------------------------------------


def test_error_a_piece_working_with_one_broken_join_never_reads_ready() -> None:
    doc = board({"J2": "broken"})
    for n in (2, 3):  # the two pieces J2 joins
        row = piece(doc, n)
        assert row.status == "working"
        assert (row.ready, row.integration, row.draw) == (
            False,
            "working_not_integrated",
            "amber",
        )
        assert row.blocking_joins == ("J2",)
        assert row.text.startswith("working, not integrated (J2 broken)")
    # A piece the broken join does not touch is not held back by it.
    assert piece(doc, 6).ready
    assert piece(doc, 4).ready


def test_error_the_board_of_2026_11_08_shows_no_piece_ready() -> None:
    """Pieces 1, 2, 3, 5, 6, 8 and 15 working, J0 and J2 to J5 broken, J1 and J8 never run: nothing is ready."""
    states = {
        "J0": "broken",
        "J1": "not_run",
        "J2": "broken",
        "J3": "broken",
        "J4": "broken",
        "J5": "broken",
        "J8": "not_run",
    }
    doc = board(states, working=(1, 2, 3, 5, 6, 8, 15))
    assert [p.piece for p in doc.pieces if p.ready] == []
    assert [
        p.piece for p in doc.pieces if p.integration == "working_not_integrated"
    ] == [
        1,
        2,
        3,
        5,
        6,
        8,
        15,
    ]
    assert doc.readiness is not None
    assert doc.readiness.ready == ()
    assert doc.readiness.working_not_integrated == (1, 2, 3, 5, 6, 8)
    assert doc.readiness.broken_joins == COUNTED
    # The summary reports ready pieces, not working ones.
    assert "0 of 9 pieces in scope are ready" in doc.subline
    assert "6 work but are not integrated" in doc.subline


def test_error_a_working_piece_with_no_join_to_a_piece_in_scope_is_not_ready() -> None:
    doc = board()
    row = piece(doc, 15)  # joins J14 and J15 reach pieces 14 and 16, outside the scope
    assert (row.status, row.ready, row.counted_joins) == ("working", False, 0)
    assert row.integration == "working_not_integrated"
    assert "outside the pieces in scope" in row.text


# --- a broken or never-run join is a red row with an owner ----------------------------------------------------------


@pytest.mark.parametrize("state", ["broken", "not_run", "golden_only", "error_only"])
@pytest.mark.parametrize("join", COUNTED)
def test_error_a_join_that_does_not_pass_is_a_red_row_with_its_owner(
    join: str, state: str
) -> None:
    doc = board({join: state})
    cause = join_cause(doc, join)
    assert cause is not None, f"{join} {state} has no row"
    assert (cause.listed, cause.state, cause.lanes) == (True, "red", ())
    assert cause.owners == (OWNERS[join],)
    assert cause.size_unit == "joins"
    # The row says what the join is and that nobody is on it.
    assert join in cause.title
    # Nobody is on the join, so the base's rule (OMN-23243) reads it as an uncarried red row: no violation, but not carried.
    assert doc.red_rule.violations == ()
    assert f"join:{join}" in doc.red_rule.uncarried_red
    assert doc.red_rule.red_rows >= 1
    row = next(j for p in doc.pieces for j in p.joins if j.id == join)
    assert (row.cause_id, row.owners, row.passes, row.counted) == (
        f"join:{join}",
        (OWNERS[join],),
        False,
        True,
    )


def test_golden_a_lane_on_the_owner_ticket_turns_the_join_row_amber_and_is_named() -> (
    None
):
    lane = ModelFloorLane(lane="join-j2-0530", ticket="OMN-23083")
    doc = board({"J2": "broken"}, lanes=(lane,))
    cause = join_cause(doc, "J2")
    assert cause is not None
    assert (cause.state, cause.lanes) == ("amber", ("join-j2-0530",))
    joined = next(j for p in doc.pieces for j in p.joins if j.id == "J2")
    assert joined.lanes == ("join-j2-0530",)
    # The other joins still have nobody on them and stay red, uncarried; the carried join is not among them.
    assert doc.red_rule.violations == ()
    assert "join:J2" not in doc.red_rule.uncarried_red


def test_error_the_colour_rule_catches_a_broken_join_with_no_owner_ticket() -> None:
    raw = overlay_text("node_floor_simple_compute")
    raw["readiness"]["in_scope_pieces"] = list(SCOPE)
    del raw["causes"]["owners"]["join:J3"]
    ov = ModelFloorSimpleOverlay.model_validate(raw)
    doc = board({"J3": "broken"}, ov=ov)
    assert doc.red_rule.state == "NOT WORKING"
    assert "join J3 has a row and no owner ticket" in doc.red_rule.violations


def test_error_the_colour_rule_catches_a_broken_join_whose_row_was_dropped() -> None:
    doc = board({"J2": "broken"})
    planted = doc.model_copy(
        update={"causes": tuple(c for c in doc.causes if c.id != "join:J2")}
    )
    reading = check_red_rule(planted, ())
    assert reading.state == "NOT WORKING"
    assert "join J2 reads broken and has no row" in reading.violations


def test_error_the_colour_rule_catches_a_ready_piece_beside_a_broken_join() -> None:
    doc = board({"J2": "broken"})
    planted = doc.model_copy(
        update={
            "pieces": tuple(
                p.model_copy(update={"ready": True}) if p.piece == 3 else p
                for p in doc.pieces
            )
        }
    )
    reading = check_red_rule(planted, ())
    assert reading.state == "NOT WORKING"
    assert "piece 3 reads ready while join J2 reads broken" in reading.violations


# --- READY appears only when every join passes -----------------------------------------------------------------------


def test_golden_every_piece_in_scope_is_ready_when_every_join_passes() -> None:
    doc = board()
    assert [p.piece for p in doc.pieces if p.ready] == list(SCOPE)
    assert all(p.draw == "green" for p in doc.pieces if p.ready)
    assert doc.readiness is not None
    assert doc.readiness.ready == SCOPE
    assert doc.readiness.broken_joins == ()
    assert "9 of 9 pieces in scope are ready" in doc.subline
    assert [c for c in doc.causes if c.id.startswith("join:")] == []
    # Piece 15 works, is outside the scope, and still is not ready.
    assert not piece(doc, 15).ready


@pytest.mark.parametrize("join", COUNTED)
def test_error_one_join_not_passing_takes_ready_from_exactly_the_pieces_it_joins(
    join: str,
) -> None:
    doc = board({join: "not_run"})
    definition = next(j for j in overlay().joins if j.id == join)
    held = {definition.from_piece, definition.to_piece}
    assert {p.piece for p in doc.pieces if p.in_scope and not p.ready} == held
    assert {p.piece for p in doc.pieces if p.ready} == set(SCOPE) - held


def test_error_a_piece_whose_own_reading_does_not_work_is_not_ready_though_its_joins_pass() -> (
    None
):
    doc = board(working=(1, 2, 3, 4, 5, 8, 9, 17))  # no reading for piece 6
    assert not piece(doc, 6).ready
    assert piece(doc, 6).integration != "ready"
    assert piece(doc, 5).ready or piece(doc, 5).blocking_joins == ()
    # The passing joins are not turned into rows because a piece does not work.
    assert [c for c in doc.causes if c.id.startswith("join:")] == []


def test_error_joins_that_cannot_be_read_never_make_a_piece_ready() -> None:
    ov = overlay()
    readings = {
        p: ModelReadingRow(
            piece=p,
            state="WORKING",
            probed_at=NOW,
            published_at=NOW,
            max_age_s=86400,
        )
        for p in SCOPE
    }
    doc = HandlerFloorSimple().handle(
        ModelFloorSimpleInput(
            now=NOW,
            overlay=ov,
            open_prs=0,
            open_prs_before=0,
            merges_in_window=0,
            watcher_read_at=NOW,
            slots_working=1,
            slots_total=2,
            record_read_at=NOW,
            lab=None,
            receipts=None,
            readings=readings,
            go_given_through=18,
        )
    )
    assert [p.piece for p in doc.pieces if p.ready] == []
    assert all(j.state == "unread" for p in doc.pieces for j in p.joins)


# --- the rule is overlay data --------------------------------------------------------------------------------------------


def test_golden_narrowing_the_scope_in_the_overlay_drops_the_joins_that_left_it() -> (
    None
):
    narrow = overlay(in_scope_pieces=[1, 2])
    doc = board({"J2": "broken", "J5": "broken"}, ov=narrow)
    # Only J1 joins two pieces of the narrowed scope; J2 and J5 reach pieces outside it and make no row.
    assert [c.id for c in doc.causes if c.id.startswith("join:")] == []
    assert [p.piece for p in doc.pieces if p.ready] == [1, 2]
    assert doc.readiness is not None
    assert doc.readiness.in_scope == (1, 2)


def test_error_widening_the_scope_in_the_overlay_counts_the_joins_that_entered_it() -> (
    None
):
    wide = overlay(in_scope_pieces=[*SCOPE, 7])
    doc = board({"J6": "not_run", "J7": "not_run"}, ov=wide)
    ids = {c.id for c in doc.causes if c.id.startswith("join:")}
    assert ids == {"join:J6", "join:J7"}
    # They have no owner in the overlay, so the colour rule says so instead of leaving them quiet.
    assert "join J6 has a row and no owner ticket" in doc.red_rule.violations


def test_error_min_joins_is_the_overlays_to_set() -> None:
    lenient = overlay(min_joins=1)
    assert not piece(board(ov=lenient), 15).ready
    raw = overlay_text("node_floor_simple_compute")
    raw["readiness"]["min_joins"] = 0
    with pytest.raises(ValueError, match="min_joins"):
        ModelFloorSimpleOverlay.model_validate(raw)


# --- which side failed ---------------------------------------------------------------------------------------------------


def test_golden_a_chain_finding_names_the_side_the_overlay_blames() -> None:
    finding = ModelChainFinding(
        code="downstream_started_after_upstream_failed",
        subject="J2 piece 2 to 3",
        correlation_id="c1",
        detail="B started although A failed",
    )
    doc = board({"J2": "broken"}, findings=(finding,))
    row = next(j for p in doc.pieces for j in p.joins if j.id == "J2")
    assert row.failed_side == "to"
    assert "downstream_started_after_upstream_failed" in row.failed_reason
    cause = join_cause(doc, "J2")
    assert cause is not None
    assert "the to side, piece 3, bus, runtime and seams" in cause.detail
    assert "Last run: 10 min ago" in cause.detail


def test_golden_a_never_run_join_blames_the_piece_that_does_not_work() -> None:
    doc = board(
        {"J1": "not_run"}, working=(2, 3, 4, 5, 6, 8, 9, 17)
    )  # piece 1 reads nothing
    row = next(j for p in doc.pieces for j in p.joins if j.id == "J1")
    assert row.failed_side == "from"
    assert "piece 1, code graph" in row.failed_reason


def test_golden_a_never_run_join_between_two_working_pieces_blames_neither() -> None:
    doc = board({"J1": "not_run"})
    row = next(j for p in doc.pieces for j in p.joins if j.id == "J1")
    assert row.failed_side == "neither"
    assert "no cycle has run both" in row.failed_reason
    cause = join_cause(doc, "J1")
    assert cause is not None
    assert "Last run: never run" in cause.detail


def test_golden_the_join_row_carries_its_definition_and_last_run() -> None:
    doc = board({"J4": "golden_only"})
    row = next(j for p in doc.pieces for j in p.joins if j.id == "J4")
    assert row.definition.startswith("overlays to secrets: piece 4")
    assert row.last_run_age_s == 600
    assert row.state == "golden_only"


# --- the rebuild strip says integrated, not working ----------------------------------------------------------------------


def test_golden_the_strip_names_the_first_piece_not_ready() -> None:
    doc = board({"J2": "broken"})
    assert doc.rebuild.current_piece == 2
    assert (
        doc.rebuild.headline == "Piece 2, hosts and capacity: working, not integrated."
    )


def test_golden_the_same_finding_in_many_chains_is_one_line_with_its_count() -> None:
    finding = ModelChainFinding(
        code="downstream_missing_typed_failure",
        subject="J2 piece 2 to 3",
        correlation_id="c1",
        detail="A failed and B gave no typed failure in 900 s",
    )
    doc = board({"J2": "broken"}, findings=(finding, finding, finding))
    row = next(j for p in doc.pieces for j in p.joins if j.id == "J2")
    assert row.failed_reason == (
        "downstream_missing_typed_failure: A failed and B gave no typed failure in 900 s (in 3 chains)"
    )
