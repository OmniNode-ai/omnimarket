# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The open piece panel says what the record can and cannot support (OMN-23049).

The panel sits over the page, so a refresh failure or an expired record drawn only in the page behind it leaves the panel
showing an old reading as current; the event window is labelled with its actual duration; a piece that is not started but
carries an event is not told it has none. The page is held by its source, the way the other panel tests hold it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PAGE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/simple.html"
).read_text(encoding="utf-8")
PANEL_CODE = PAGE.split("// ---- The rebuild strip:")[1].split("// ---- Tabs.")[0]


def function_body(name: str) -> str:
    head = PAGE.split(f"function {name}(")[1]
    return head.split("\n}\n")[0]


def test_error_the_open_panel_carries_the_refresh_failure_and_expiry_warning() -> None:
    body = function_body("renderPanel")
    assert "recordWarning(" in body
    assert 'getElementById("pbody").innerHTML = recordWarning(' in body
    warning = function_body("recordWarning")
    assert "The last refresh failed" in warning
    assert "The floor is frozen" in warning


def test_golden_the_page_and_the_panel_draw_one_warning_and_it_clears_with_fresh_data() -> (
    None
):
    # draw() rebuilds both the page and the open panel every ten seconds and after each load, so a warning that is a pure
    # function of (failure, age) leaves with the failure and with the expired record.
    assert "h += recordWarning(age, limit);" in function_body("draw")
    assert "else if (openPiece != null) renderPanel();" in PAGE
    assert 'failure = ""' in function_body("load")


def test_error_the_event_window_label_is_not_rounded_into_another_interval() -> None:
    assert "Math.round(p.window_s / 3600)" not in PAGE
    assert "windowText(p.window_s)" in PAGE
    label = function_body("windowText")
    assert "Math.round" not in label
    for unit in ('"minute"', '"hour"', '"second"', '"day"'):
        assert unit in label


def test_error_a_not_started_piece_with_events_is_not_told_it_has_none() -> None:
    branch = next(
        line
        for line in PANEL_CODE.splitlines()
        if line.lstrip().startswith('if (p.status === "not_started") return h +')
    )
    assert "hasEvents(p)" in branch
    assert "eventsSection(p)" in branch
    assert "hasEvents" in PANEL_CODE


def test_golden_a_not_started_piece_with_no_event_keeps_the_minimal_view() -> None:
    assert "Not started. It has no reading and no events yet." in PANEL_CODE
