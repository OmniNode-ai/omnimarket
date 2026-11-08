# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A piece's event counts are published only where the receipts behind them were read and reach back the whole window (OMN-23049).

A count over a window longer than the loaded history, or a count for a piece whose ledger events could not be read, is
unread (``None``), never a measured zero.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)
from tests.nodes.node_floor_simple_compute.test_piece_panel_cells_floor_simple_compute import (
    WINDOW_S,
    chain,
    piece,
    with_receipts,
)

pytestmark = pytest.mark.unit


def test_error_a_window_longer_than_the_loaded_history_is_unread_not_a_count() -> None:
    base = with_receipts((chain(2, "golden", 60, "a"), chain(2, "error", 120, "b")))
    short = base.model_copy(update={"receipts_covered_s": WINDOW_S - 1})
    p2 = piece(HandlerFloorSimple().handle(short), 2)
    assert (p2.golden_in_window, p2.error_in_window) == (None, None)
    assert p2.window_s == WINDOW_S


def test_golden_a_window_the_loaded_history_covers_is_counted() -> None:
    base = with_receipts((chain(2, "golden", 60, "a"), chain(2, "error", 120, "b")))
    exact = base.model_copy(update={"receipts_covered_s": WINDOW_S})
    p2 = piece(HandlerFloorSimple().handle(exact), 2)
    assert (p2.golden_in_window, p2.error_in_window) == (1, 1)


def test_error_a_piece_without_local_chains_is_unread_when_the_ledger_was_not_read() -> (
    None
):
    base = with_receipts((chain(2, "golden", 60, "a"),))
    down = base.model_copy(update={"sources_read": {"ledger": False}})
    doc = HandlerFloorSimple().handle(down)
    assert (piece(doc, 3).golden_in_window, piece(doc, 3).error_in_window) == (
        None,
        None,
    )


def test_golden_a_piece_with_local_chains_is_still_counted_when_the_ledger_was_not_read() -> (
    None
):
    base = with_receipts((chain(2, "golden", 60, "a"),))
    down = base.model_copy(update={"sources_read": {"ledger": False}})
    p2 = piece(HandlerFloorSimple().handle(down), 2)
    assert (p2.golden_in_window, p2.error_in_window) == (1, 0)


def test_golden_a_piece_without_local_chains_is_a_measured_zero_when_the_ledger_was_read() -> (
    None
):
    base = with_receipts((chain(2, "golden", 60, "a"),))
    up = base.model_copy(update={"sources_read": {"ledger": True}})
    p3 = piece(HandlerFloorSimple().handle(up), 3)
    assert (p3.golden_in_window, p3.error_in_window) == (0, 0)
