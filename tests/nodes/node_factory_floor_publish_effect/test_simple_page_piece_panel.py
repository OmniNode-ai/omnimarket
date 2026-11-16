# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The main page's rebuild strip opens a tooltip and a side panel, read from the floor record (OMN-22810).

The behaviour (hover, click, Esc, outside click, Back, no reflow, phone and desktop) was run in a browser against the served
page and its screenshots are in the pull request; these tests hold the parts of the page that carry it, so a later edit
cannot drop one without a red test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PAGE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/simple.html"
).read_text(encoding="utf-8")


def test_golden_each_piece_on_the_strip_is_a_link_to_its_own_address() -> None:
    assert 'href="#piece-${p.piece}" data-piece="${p.piece}"' in PAGE
    assert "history.pushState({piece: n}" in PAGE
    assert re.search(r"/\^#piece-\(\\d\{1,2\}\)\$/", PAGE)


def test_golden_the_panel_is_a_dialog_that_closes_four_ways() -> None:
    assert 'setAttribute("role", "dialog")' in PAGE
    assert 'e.key === "Escape"' in PAGE
    assert 'back.addEventListener("click", closePanel)' in PAGE
    assert 'e.target.id === "pclose"' in PAGE
    assert 'window.addEventListener("popstate", syncPanel)' in PAGE


def test_golden_the_panel_slides_from_the_right_and_from_the_bottom_on_a_phone() -> (
    None
):
    assert "transform:translateX(100%)" in PAGE
    assert "@media(max-width:600px){#ppanel{" in PAGE
    assert "transform:translateY(100%)" in PAGE
    assert "prefers-reduced-motion" in PAGE


def test_error_opening_the_panel_does_not_lock_or_resize_the_page_body() -> None:
    # Hiding the body's overflow removes the scrollbar and moves the page: the panel is fixed and overlays instead.
    assert "overflow:hidden" not in PAGE
    assert "document.body.style" not in PAGE
    assert "#ppanel{position:fixed" in PAGE


def test_error_the_redraw_every_ten_seconds_keeps_focus_and_does_not_close_the_panel() -> (
    None
):
    assert 'getAttribute("data-fk")' in PAGE
    assert "else if (openPiece != null) renderPanel();" in PAGE
    assert "document.body.append(tip, back, panel)" in PAGE


def test_error_a_piece_not_started_shows_only_its_name_and_what_it_will_do() -> None:
    assert 'if (p.status === "not_started") return h + ' in PAGE


def test_golden_the_panel_reads_only_cells_of_the_record_the_page_already_loads() -> (
    None
):
    for cell in (
        "p.what",
        "p.reason",
        "p.next_text",
        "p.golden_in_window",
        "p.error_in_window",
        "p.window_s",
        "doc.causes",
    ):
        assert cell in PAGE
    # The panel's own code (from its section comment to the tabs section) reads nothing: no fetch, no new source.
    panel_code = PAGE.split("// ---- The rebuild strip:")[1].split("// ---- Tabs.")[0]
    assert "fetch(" not in panel_code


def test_golden_the_page_says_working_not_integrated_and_reports_the_ready_count() -> (
    None
):
    """OMN-23522: a piece that works and has a join that does not pass is worded as not integrated."""
    assert 'working_not_integrated: "Working, not integrated"' in PAGE
    assert "const pieceWord = p => INTEGRATION_WORD[p.integration]" in PAGE
    # The strip label, the hover note and the side panel all word the piece through the one function.
    assert PAGE.count("pieceWord(p)") >= 3
    assert "Ready, not just working:" in PAGE
    assert "doc.readiness.text" in PAGE
    # The piece's note names the joins that do not pass; a join that does not count says so.
    assert "p.blocking_joins" in PAGE
    assert '" (outside the scope)"' in PAGE


def test_error_a_piece_that_does_not_work_keeps_its_own_word_and_a_missing_readiness_draws_no_line() -> (
    None
):
    # Any integration value the table lacks (not_working, not_started) falls back to the reading's own word.
    assert "|| STATUS_WORD[p.status] || p.status;" in PAGE
    # A record written before the rule existed carries no readiness: the page draws no ready line for it.
    assert "if (doc.readiness) h +=" in PAGE
    # The strip label no longer takes the reading's word past the integration word.
    assert "(STATUS_WORD[p.status] || p.status).toLowerCase()" not in PAGE
