# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The main page's rebuild strip opens a tooltip and a side panel, read from the floor record (OMN-22810, OMN-23018).

The behaviour (hover, click, Esc, outside click, Back, no reflow, phone and desktop) was run in a browser against the served
page and its screenshots are in the pull request; these tests hold the parts of the page that carry it, so a later edit
cannot drop one without a red test. Since OMN-23018 the panel itself is the shared component (page/shared/floor-panel.js and
floor-panel.css, also read by the Better or worse page): the dialog, the address, the close and the slide are asserted on
those files, and what each key shows is asserted on the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect/page"
)
PAGE = (NODE / "simple.html").read_text(encoding="utf-8")
JS = (NODE / "shared/floor-panel.js").read_text(encoding="utf-8")
CSS = (NODE / "shared/floor-panel.css").read_text(encoding="utf-8")


def test_golden_each_piece_on_the_strip_is_a_link_to_its_own_address() -> None:
    assert (
        'href="#piece-${p.piece}" data-piece="${p.piece}" data-open="piece:${p.piece}"'
        in PAGE
    )
    assert "history.pushState({fp: s.key" in JS
    assert re.search(r"/\^#\(\[a-z\]\+\)-\(\.\+\)\$/", JS)


def test_golden_the_panel_is_a_dialog_that_closes_four_ways() -> None:
    assert 'setAttribute("role", "dialog")' in JS
    assert 'e.key === "Escape"' in JS
    assert 'back.addEventListener("click", close)' in JS
    assert 'e.target.id === "fp-close"' in JS
    assert 'window.addEventListener("popstate", sync)' in JS


def test_golden_the_panel_slides_from_the_right_and_from_the_bottom_on_a_phone() -> (
    None
):
    assert "transform:translateX(100%)" in CSS
    assert "@media(max-width:600px){#fp-panel{" in CSS
    assert "transform:translateY(100%)" in CSS
    assert "prefers-reduced-motion" in CSS


def test_error_opening_the_panel_does_not_lock_or_resize_the_page_body() -> None:
    # Hiding the body's overflow removes the scrollbar and moves the page: the panel is fixed and overlays instead.
    for source in (PAGE, JS, CSS):
        assert "overflow:hidden" not in source
        assert "document.body.style" not in source
    assert "#fp-panel{position:fixed" in CSS


def test_error_the_redraw_every_ten_seconds_keeps_focus_and_does_not_close_the_panel() -> (
    None
):
    assert 'getAttribute("data-fk")' in PAGE
    assert "FloorPanel.update();" in PAGE
    # The open panel is refreshed from the new record in place; it is never closed by a redraw.
    update = JS.split("function update()")[1].split("\n  }\n")[0]
    assert "hide()" in update
    assert "render()" in update
    assert "document.body.append(back, panel)" in JS


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
        "doc.details",
        "doc.health",
    ):
        assert cell in PAGE
    # The panel's own code (from its section comment to the tabs section) reads nothing: no fetch, no new source.
    panel_code = PAGE.split("// ---- The rebuild strip:")[1].split("// ---- Tabs.")[0]
    assert "fetch(" not in panel_code
    assert "fetch(" not in JS


def test_golden_every_number_row_and_piece_opens_a_panel_by_its_key() -> None:
    # OMN-23018: the three numbers, the colour rule's red rows, the alarms row, the holdup rows, the lab lines, the health
    # line and the emission chains line each carry the key of the panel they open.
    for marker in (
        'data-open="number:${esc(n.key)}"',
        'data-open="number:red_rows"',
        'data-open="alarms:open"',
        'data-open="cause:${esc(c.id)}"',
        'data-open="lab:${esc(l.id)}"',
        'data-open="health:line"',
        'data-open="cause:chains:broken"',
        'data-open="piece:${p.piece}"',
    ):
        assert marker in PAGE, marker
    assert '<link rel="stylesheet" href="floor-panel.css">' in PAGE
    assert '<script src="floor-panel.js"></script>' in PAGE
