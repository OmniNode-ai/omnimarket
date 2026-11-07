# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cells the main page's piece side panel reads from the floor record (OMN-22810).

Each piece row carries the overlay's sentence, the reading's own reason without the event ages, the step its state calls
for, and the golden and error chains that ended inside the overlay's window. Unread receipts are ``None``, never zero.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.floor_17a import (
    ModelChain,
    ModelFloorPieceRow,
    ModelFloorReceipts,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelPieceReceipt,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)
from tests.nodes.node_floor_simple_compute.test_golden_and_error_chain_floor_simple_compute import (
    NOW,
    floor_input,
)

pytestmark = pytest.mark.unit

WINDOW_S = 86400


def chain(piece: int, kind: str, ended_ago_s: int, cid: str) -> ModelChain:
    return ModelChain.model_validate(
        {
            "piece": piece,
            "node": "node_x",
            "correlation_id": cid,
            "kind": kind,
            "started_at": NOW - timedelta(seconds=ended_ago_s + 5),
            "ended_at": NOW - timedelta(seconds=ended_ago_s),
            "backend": "memory",
            "drill": False,
            "counters": {},
        }
    )


def with_receipts(
    chains: tuple[ModelChain, ...], go: int | None = 3
) -> ModelFloorSimpleInput:
    base = floor_input(go_given_through=go)
    receipts = ModelFloorReceipts(
        computed_at=NOW,
        pieces=tuple(
            ModelPieceReceipt(
                piece=n,
                started=False,
                golden_at=None,
                error_at=None,
                golden_backends=(),
                last_emission_at=None,
                golden_age_s=None,
                error_age_s=None,
                freshness="none",
            )
            for n in range(1, 19)
        ),
        joins=(),
        chains=chains,
        findings=(),
    )
    return base.model_copy(update={"receipts": receipts})


def piece(doc: ModelFloorSimple, n: int) -> ModelFloorPieceRow:
    return next(p for p in doc.pieces if p.piece == n)


def test_golden_a_piece_row_carries_the_overlay_sentence_and_the_window_counts() -> (
    None
):
    doc = HandlerFloorSimple().handle(
        with_receipts(
            (
                chain(2, "golden", 60, "a"),
                chain(2, "golden", 3600, "b"),
                chain(2, "error", 120, "c"),
                chain(3, "golden", 60, "d"),
            )
        )
    )
    p2 = piece(doc, 2)
    assert p2.what.startswith("The list of lab machines")
    assert (p2.golden_in_window, p2.error_in_window, p2.window_s) == (2, 1, WINDOW_S)
    assert (piece(doc, 3).golden_in_window, piece(doc, 3).error_in_window) == (1, 0)


def test_error_a_chain_that_ended_outside_the_window_is_not_counted() -> None:
    doc = HandlerFloorSimple().handle(
        with_receipts(
            (
                chain(2, "golden", WINDOW_S - 1, "in"),
                chain(2, "golden", WINDOW_S + 1, "out"),
                chain(2, "error", WINDOW_S + 60, "out-error"),
            )
        )
    )
    p2 = piece(doc, 2)
    assert (p2.golden_in_window, p2.error_in_window) == (1, 0)


def test_error_broken_and_open_chains_are_not_counted_as_golden_or_error() -> None:
    doc = HandlerFloorSimple().handle(
        with_receipts((chain(2, "broken", 60, "x"), chain(2, "open", 60, "y")))
    )
    assert (piece(doc, 2).golden_in_window, piece(doc, 2).error_in_window) == (0, 0)


def test_error_unread_receipts_give_none_not_zero() -> None:
    doc = HandlerFloorSimple().handle(floor_input())
    p1 = piece(doc, 1)
    assert (p1.golden_in_window, p1.error_in_window) == (None, None)


def test_golden_the_reason_is_the_reading_without_the_event_ages() -> None:
    # A golden chain makes the row's text carry "golden ..., error ..."; the reason is the text before it.
    doc = HandlerFloorSimple().handle(
        with_receipts((chain(2, "golden", 60, "a"),)).model_copy(
            update={"go_given_through": 3}
        )
    )
    p = piece(doc, 2)
    assert p.reason
    assert "golden" not in p.reason
    assert p.text.startswith(p.reason)


def test_golden_a_piece_not_started_says_it_waits_for_the_go() -> None:
    doc = HandlerFloorSimple().handle(floor_input(go_given_through=0))
    waiting = [p for p in doc.pieces if p.status == "not_started"]
    assert waiting
    assert all(p.next_text.startswith("Waits for the go") for p in waiting)
