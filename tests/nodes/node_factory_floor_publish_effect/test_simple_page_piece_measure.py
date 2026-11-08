# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The piece panel shows the piece's measure against the old way, read from the comparison record (OMN-23081)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PAGE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/simple.html"
).read_text(encoding="utf-8")


def test_golden_the_panel_reads_each_piece_measure_from_the_comparison_record() -> None:
    assert 'const COMPARE_DATA = "floor-compare.json";' in PAGE
    assert "for (const pm of (cd.pieces || [])) pieceMeasures[pm.piece] = pm;" in PAGE
    assert "`<h3>Against the old way, 7 days</h3>` + measureSection(p)" in PAGE
    for word in ("better:", "worse:", "unread:", "pending:"):
        assert word in PAGE.split("const VERDICT_WORD = ", 1)[1].split("\n", 1)[0]


def test_error_a_comparison_record_that_cannot_be_read_says_so_and_infers_nothing() -> (
    None
):
    assert "The comparison record cannot be read (" in PAGE
    assert 'const num = (v, u) => v == null ? "unread"' in PAGE
    assert "no 7-day comparison for this piece yet" not in PAGE
