# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick tells the floor how far back its loaded receipts reach, so a longer event window reads as unread (OMN-23049)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_floor_simple_effect.support import OVERLAYS, build_home
from tests.nodes.node_floor_simple_effect.test_piece_events_overlay_floor_simple import (
    GO_RULINGS,
    PIECE_ROWS,
    PIECES,
    lines,
    run_tick,
)

pytestmark = pytest.mark.unit

LONG_WINDOW_S = 604800


def overlay_with_window(tmp_path: Path, window_s: int) -> Path:
    root = tmp_path / "overlays-window"
    source = OVERLAYS / "node_floor_simple_compute" / "overlay.yaml"
    target = root / "node_floor_simple_compute" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    text = source.read_text(encoding="utf-8")
    marker = "  current_piece: 1\n"
    assert marker in text
    target.write_text(
        text.replace(marker, f"{marker}  event_window_s: {window_s}\n", 1),
        encoding="utf-8",
    )
    for other in OVERLAYS.iterdir():
        if other.name != "node_floor_simple_compute":
            (root / other.name).symlink_to(other, target_is_directory=True)
    return root


def test_error_a_window_longer_than_the_local_history_is_published_as_unread(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger([*lines(GO_RULINGS), *lines(PIECE_ROWS)])
    doc = run_tick(
        build_home(tmp_path),
        ledger,
        overlay_with_window(tmp_path, LONG_WINDOW_S),
    )
    by_piece = {p.piece: p for p in doc.pieces}
    for n in PIECES:
        assert by_piece[n].window_s == LONG_WINDOW_S
        assert by_piece[n].golden_in_window is None, n
        assert by_piece[n].error_in_window is None, n


def test_golden_a_window_inside_the_local_history_is_still_counted(
    tmp_path: Path,
) -> None:
    ledger = InMemoryLedger([*lines(GO_RULINGS), *lines(PIECE_ROWS)])
    doc = run_tick(build_home(tmp_path), ledger, OVERLAYS)
    by_piece = {p.piece: p for p in doc.pieces}
    assert all(by_piece[n].golden_in_window is not None for n in PIECES)
