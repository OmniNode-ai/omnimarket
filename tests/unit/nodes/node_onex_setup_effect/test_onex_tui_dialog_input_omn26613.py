# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typing in the setup dialog's key or URL field must not crash the TUI (OMN-26613).

App queries run against the default (bottom) screen, which holds #palette. The
app's Input.Changed handler queried it for every Input in the app, including
#key and #url in the Config tab, so it raised NoMatches once the main screen
was gone from the stack. A tab swap still reading when Esc closed the tab could
do that: switch_screen popped the main screen.

Failure modes:
  N1/N2  the main screen has no #palette, then one character in #key / #url
         (NoMatches on dev).
  K1/K2  Esc during a tab swap, then the swap lands, then the Config tab and a
         character in #key / #url (ScreenStackError on dev).
  S1/S2  one character in #key / #url still starts the model search (control).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Input

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import OnexTui, SetupScreen
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _open_config,
    _Runner,
)

pytestmark = pytest.mark.unit

_FIELDS = ["key", "url"]


async def _focus_field(app: OnexTui, pilot: Pilot[Any], field: str) -> Input:
    """Show and focus #key (a hosted provider) or #url (the local one)."""
    screen = app.screen
    assert isinstance(screen, SetupScreen)
    screen.query_one("#provider").value = (
        cli_tui._picker()[0].provider if field == "key" else "local"
    )
    await pilot.pause()
    widget = screen.query_one(f"#{field}", Input)
    widget.focus()
    await pilot.pause()
    assert app.focused is widget
    return widget


async def _swap_closed_by_esc(
    app: OnexTui, pilot: Pilot[Any], patch: pytest.MonkeyPatch
) -> None:
    """Start a swap to another tab, close the tab before the swap lands."""
    gate = asyncio.Event()
    real = app.tab_screen

    async def slow(name: str) -> Any:  # the next tab's read is still running
        await gate.wait()
        return await real(name)

    patch.setattr(app, "tab_screen", slow)
    await pilot.press("tab")  # Config -> Model: the swap waits on its read
    await pilot.pause()
    await pilot.press("escape")  # the user closes the tab meanwhile
    await pilot.pause()
    gate.set()
    for _ in range(4):
        await pilot.pause()
    patch.setattr(app, "tab_screen", real)


@pytest.mark.parametrize("field", _FIELDS)
async def test_typing_with_no_palette_on_the_main_screen_keeps_the_tui_running(
    field: str,
) -> None:
    """N1/N2: the ticket's crash, made directly."""
    app = _app(_Runner([]))
    async with app.run_test(size=(140, 44)) as pilot:
        await _open_config(pilot, app)
        await app.default_screen.query_one("#palette").remove()
        widget = await _focus_field(app, pilot, field)
        await pilot.press("a")
        await pilot.pause()
        assert app.is_running
        assert widget.value.endswith("a")


@pytest.mark.parametrize("field", _FIELDS)
async def test_typing_after_a_tab_swap_raced_esc_keeps_the_tui_running(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """K1/K2: the real route to a missing main screen."""
    app = _app(_Runner([]))
    async with app.run_test(size=(140, 44)) as pilot:
        await _open_config(pilot, app)
        await _swap_closed_by_esc(app, pilot, monkeypatch)
        assert app.is_running
        assert len(app.screen_stack) >= 2  # the main screen is still under a tab
        for _ in range(3):  # Model -> Delegation -> Dashboard -> Config
            await pilot.press("tab")
            for _ in range(3):
                await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        widget = await _focus_field(app, pilot, field)
        await pilot.press("a")
        await pilot.pause()
        assert app.is_running
        assert widget.value.endswith("a")
        assert app.default_screen.query("#palette")  # the main screen kept it


@pytest.mark.parametrize("field", _FIELDS)
async def test_typing_in_key_and_url_starts_the_model_search(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1/S2: one character still schedules the search, which then runs."""
    runs: list[str] = []
    real = SetupScreen._auto_find

    def spy(self: SetupScreen) -> None:
        runs.append(field)
        real(self)

    monkeypatch.setattr(SetupScreen, "_auto_find", spy)
    app = _app(_Runner([]))
    async with app.run_test(size=(140, 44)) as pilot:
        await _open_config(pilot, app)
        await _focus_field(app, pilot, field)
        await pilot.pause(1.5)  # let the searches the setup itself started run
        runs.clear()
        await pilot.press("a")
        await pilot.pause(1.5)  # auto_find_seconds is 0.8
        assert runs == [field]
