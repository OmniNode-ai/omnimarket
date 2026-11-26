"""OMN-26620: the four tabs share one tidy layout at every size.

Operator 2026-11-25 (screenshot of /config): "these 4 tabs and inside should be proper
and nice - no mistake". Failure modes seen on the real screens:
  L1  /config's buttons float on their own row above the key hints, mid-screen,
      or a narrow window cuts the hints off to fit the buttons.
  L2  /config says "none yet" in green, the colour of a success.
  L3  /model's Auto button and its key hints sit on separate rows.
  L4  /delegation's switch, hook and Claude Code lines float unboxed, and its
      Save/Cancel sit on a row of their own above the hints.
  L5  /dashboard says nothing about what the dashboard is.
"""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _Runner,
    _selected_rows,
    _type,
)

_SIZES = [(140, 46), (100, 34)]


async def _rows(
    command: str,
    size: tuple[int, int],
    panel: str = "",
    probe: str = "",
    rows_by_command: dict[tuple[str, ...], str] | None = None,
) -> tuple[list[str], int, bool]:
    """The screen's rows, the panel's last inner row, and whether ``probe`` is empty-marked."""
    listing = rows_by_command or _selected_rows(connection=False)
    app = _app(_Runner([], by_command=listing))
    async with app.run_test(size=size) as pilot:
        await _type(pilot, "/model" if command == "+" else command)
        await pilot.press("enter")
        for _ in range(10):
            await pilot.pause(0.05)
        if command == "+":  # the Model tab's add-models screen (OMN-26688)
            await app.open_add_models()
            for _ in range(10):
                await pilot.pause(0.05)
        rows = [
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()
        ]
        last = app.screen.query_one(panel).region.bottom - 2 if panel else -1
        empty = False
        if probe:
            widget = app.screen.query_one(probe)
            text = str(widget.render()).strip()
            empty = text != tui_design().words["basket_empty"] or widget.has_class(
                "-empty"
            )
        return rows, last, empty


def _row_with(rows: list[str], *words: str) -> int | None:
    return next((i for i, r in enumerate(rows) if all(w in r for w in words)), None)


@pytest.mark.parametrize("size", _SIZES)
async def test_config_footer_is_one_row_at_the_bottom(size: tuple[int, int]) -> None:
    """L1."""
    rows, last, _ = await _rows("+", size, "#form")
    hints = _row_with(rows, "space tick · ctrl+s save · esc back to providers")
    buttons = _row_with(rows, "Save", "Cancel")
    assert hints is not None, "\n".join(rows)
    assert buttons is not None, "\n".join(rows)
    # one row when it fits; in a narrow window the hints sit right above the buttons
    assert buttons == last, (buttons, last)
    assert hints in (last, last - 1), (hints, last)


async def test_config_none_yet_is_not_green() -> None:
    """L2."""
    _, _, ok = await _rows("+", (140, 46), probe="#basket")
    assert ok


@pytest.mark.parametrize("size", _SIZES)
async def test_model_hints_and_auto_share_the_bottom_row(size: tuple[int, int]) -> None:
    """L3. Use Auto shows only with a model pinned first, so one is pinned."""
    pinned = json.dumps(
        {
            "provider": "local",
            "set_up": True,
            "models": ["llama3.2"],
            "tried_first": True,
            "first_model": "llama3.2",
        }
    )
    rows, last, _ = await _rows(
        "/model", size, "#pick", rows_by_command={("models", "list"): pinned}
    )
    row = _row_with(rows, "esc close", "Auto")
    assert row is not None, "\n".join(rows)
    assert row == last, (row, last)
    assert _row_with(rows, "Your models") is not None


@pytest.mark.parametrize("size", _SIZES)
async def test_delegation_is_boxed_with_one_footer_row(size: tuple[int, int]) -> None:
    """L4."""
    rows, last, _ = await _rows("/delegation", size, "#deleg")
    words = tui_design().words
    title = _row_with(rows, tui_design().delegation.title)
    assert title is not None
    assert "╭" in rows[title - 1], rows[title - 1]  # the state box opens above it
    assert _row_with(rows, words["order_title"]) is not None
    row = _row_with(rows, "esc close", "Save", "Cancel")
    assert row is not None, "\n".join(rows)
    assert row == last, (row, last)


async def test_dashboard_says_what_it_is() -> None:
    """L5."""
    rows, _, _ = await _rows("/dashboard", (140, 46))
    about = tui_design().words["dashboard_about"]
    assert about.split(".")[0] in " ".join(" ".join(rows).split())
