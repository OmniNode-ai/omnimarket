# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""READY means real factory work crossed the joins and no neighbour reads NOT WORKING (OMN-23862).

On 2026-11-11 pieces 1, 2, 4, 5, 6 and 7 read READY from the factory's own probe traffic alone, two of them beside piece 3, which
read NOT WORKING. Each control below fails on that board and passes only on the rule:

- a join whose golden chain was probe traffic passes as health and never counts toward READY;
- a piece is not READY while a neighbour on a counted join reads NOT WORKING, and the board says so in words;
- the two readiness values (the origins that are real, the neighbour statuses that block) are the overlay's, never the code's;
- the readiness sentence names every in-scope piece, the ones that do not work included;
- every input of the rule is on the input model; the rule opens no file.
"""

from __future__ import annotations

import ast
import inspect
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.floor_17a import (
    ModelFloorReceipts,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
    ModelJoinReceipt,
    ModelReadingRow,
)
from omnimarket.nodes.node_floor_simple_compute.handlers import handler_floor_simple
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
    check_red_rule,
)
from tests.nodes.node_floor_simple_compute.test_readiness_omn23081 import (
    COUNTED,
    NOW,
    overlay,
    piece,
    receipts,
)

pytestmark = pytest.mark.unit

ALL_WORKING = (1, 2, 3, 4, 5, 6, 8, 9, 17)


def real_receipts(ov: ModelFloorSimpleOverlay, real: set[str]) -> ModelFloorReceipts:
    """Every counted join passes; only the joins in ``real`` had a golden chain from real work."""
    base = receipts(ov)
    joins = tuple(
        ModelJoinReceipt.model_validate(
            j.model_dump()
            | {
                "real_golden_at": (NOW - timedelta(minutes=10))
                if j.join in real
                else None
            }
        )
        for j in base.joins
    )
    return base.model_copy(update={"joins": joins})


def board(
    real: set[str] | None = None,
    *,
    ov: ModelFloorSimpleOverlay | None = None,
    working: tuple[int, ...] = ALL_WORKING,
    not_working: tuple[int, ...] = (),
    covered_s: int | None = None,
    folded: ModelFloorReceipts | None = None,
) -> ModelFloorSimple:
    """The board of ``real`` joins planted on the receipts, or of ``folded`` receipts the emission fold produced."""
    ov = ov or overlay()
    readings = {
        p: ModelReadingRow(
            piece=p,
            state="WORKING" if p in working else "NOT WORKING",
            probed_at=NOW - timedelta(minutes=5),
            published_at=NOW - timedelta(minutes=5),
            max_age_s=86400,
        )
        for p in (*working, *not_working)
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
            lab=None,
            receipts=folded
            if folded is not None
            else real_receipts(ov, set(COUNTED) if real is None else real),
            receipts_covered_s=covered_s,
            readings=readings,
            go_given_through=18,
        )
    )


def ready(doc: ModelFloorSimple) -> list[int]:
    return [p.piece for p in doc.pieces if p.ready]


# --- real work decides READY ------------------------------------------------------------------------------------------


def test_golden_a_real_golden_chain_on_every_counted_join_reads_ready() -> None:
    doc = board()
    assert ready(doc) == [1, 2, 3, 4, 5, 6, 8, 9, 17]
    assert doc.readiness is not None
    assert doc.readiness.probe_only_joins == ()
    assert piece(doc, 2).text.startswith(
        "READY: reading WORKING and J1, J2 pass on real work"
    )


def test_error_a_probe_only_golden_chain_never_reads_ready() -> None:
    doc = board(set(COUNTED) - {"J1"})
    for n in (1, 2):  # the two pieces J1 joins
        row = piece(doc, n)
        assert (row.ready, row.integration) == (False, "working_not_integrated")
        assert row.probe_only_joins == ("J1",)
        assert row.text.startswith(
            "working, not integrated (green on probes only: no real work crossed this join in 24 hours (J1))"
        )
    assert ready(doc) == [3, 4, 5, 6, 8, 9, 17]


def test_error_the_hours_in_the_reason_come_from_the_window_the_receipts_cover() -> (
    None
):
    doc = board(set(), covered_s=48 * 3600)
    assert (
        "green on probes only: no real work crossed these joins in 48 hours (J0, J1)"
        in piece(doc, 1).text
    )


def test_error_a_golden_chain_older_than_the_window_is_not_real_work() -> None:
    ov = overlay()
    base = real_receipts(ov, set(COUNTED))
    stale = tuple(
        j.model_copy(update={"real_golden_at": NOW - timedelta(hours=30)})
        if j.join == "J1"
        else j
        for j in base.joins
    )
    doc = board(set(COUNTED), ov=ov, covered_s=24 * 3600)
    assert ready(doc) == [1, 2, 3, 4, 5, 6, 8, 9, 17]
    readings = {
        p: ModelReadingRow(
            piece=p,
            state="WORKING",
            probed_at=NOW - timedelta(minutes=5),
            published_at=NOW - timedelta(minutes=5),
            max_age_s=86400,
        )
        for p in ALL_WORKING
    }
    old = HandlerFloorSimple().handle(
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
            receipts=base.model_copy(update={"joins": stale}),
            receipts_covered_s=24 * 3600,
            readings=readings,
            go_given_through=18,
        )
    )
    assert piece(old, 1).probe_only_joins == ("J1",)
    assert 1 not in ready(old)


# --- a neighbour that is not working ------------------------------------------------------------------------------------


def test_error_a_working_piece_beside_a_not_working_neighbour_is_not_ready() -> None:
    doc = board(working=(1, 2, 4, 5, 6, 8, 9, 17), not_working=(3,))
    for n in (2, 4):  # the neighbours of piece 3 over J2 and J3
        row = piece(doc, n)
        assert (row.ready, row.integration) == (False, "working_not_integrated")
        assert row.blocking_neighbours == (3,)
        assert "neighbour piece 3 reads NOT WORKING" in row.text
    assert ready(doc) == [1, 5, 6, 8, 9, 17]


# --- the values are the overlay's ---------------------------------------------------------------------------------------


def test_error_the_overlay_values_decide_the_result() -> None:
    working = (1, 2, 4, 5, 6, 8, 9, 17)
    assert 2 not in ready(board(working=working, not_working=(3,)))
    unblocked = overlay(blocking_neighbour_statuses=["unknown"])
    assert 2 in ready(board(ov=unblocked, working=working, not_working=(3,)))
    # With no origin counted as real, nothing is real work, whatever the chains say.
    assert ready(board(ov=overlay(real_origins=["probe"]))) == ready(board())


@pytest.mark.parametrize("key", ["real_origins", "blocking_neighbour_statuses"])
def test_error_a_readiness_block_without_either_key_is_refused(key: str) -> None:
    from tests.nodes.node_floor_simple_effect.support import overlay_text

    raw: dict[str, Any] = overlay_text("node_floor_simple_compute")
    del raw["readiness"][key]
    with pytest.raises(ValidationError, match=key):
        ModelFloorSimpleOverlay.model_validate(raw)


# --- probe health stays visible, apart --------------------------------------------------------------------------------


def test_golden_probe_health_is_shown_apart_and_never_counts() -> None:
    doc = board(set())
    assert doc.readiness is not None
    assert doc.readiness.ready == ()
    assert doc.readiness.probe_health_joins == COUNTED
    assert doc.readiness.probe_only_joins == COUNTED
    assert (
        "Probe health: 7 of 7 counted joins pass on probe chains"
        in doc.readiness.probe_health_text
    )
    assert doc.readiness.probe_health_text in doc.subline
    joins = [j for p in doc.pieces for j in p.joins if j.counted]
    assert all(j.passes and not j.real_passes for j in joins)


# --- the sentence names every piece in scope ---------------------------------------------------------------------------


def test_golden_the_readiness_sentence_names_every_in_scope_piece() -> None:
    doc = board(working=(1, 2, 4, 5, 6, 8, 9, 17), not_working=(3,))
    assert doc.readiness is not None
    assert doc.readiness.not_working == (3,)
    assert "1 do not work (piece 3 not working)" in doc.readiness.text
    for n in doc.readiness.in_scope:
        assert f"{n}" in doc.readiness.text


# --- the rule reads its inputs from the model, never a file ------------------------------------------------------------


def test_error_the_red_rule_reads_a_ready_piece_over_a_probe_only_join() -> None:
    doc = board(set())
    forged = doc.model_copy(
        update={
            "pieces": tuple(
                p.model_copy(update={"ready": True}) if p.piece == 1 else p
                for p in doc.pieces
            )
        }
    )
    assert any("saw no real work" in v for v in check_red_rule(forged, ()).violations)


def test_error_the_readiness_rule_takes_no_file_source() -> None:
    """Every input of the rule (real-chain evidence, neighbour reading, origin) is on the input model; no file is opened."""
    tree = ast.parse(inspect.getsource(handler_floor_simple))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {"pathlib", "io", "json", "builtins"}
    opened = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open"
    ]
    assert opened == []
    assert list(inspect.signature(HandlerFloorSimple().handle).parameters) == [
        "request"
    ]
