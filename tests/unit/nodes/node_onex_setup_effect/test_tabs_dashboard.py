# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dashboard tab, mockup page 7 (OMN-26620 AC12).

Failure modes this suite is written against (enumerated before the code):
  E1  the title has no state beside it, or the pid is missing while running.
  E2  the address is not on a line of its own, or shows while stopped.
  E3  the buttons are not the mockup's: Open in browser and Stop while
      running, Start while stopped; a Cancel button or both sets at once.
  E4  a stopped dashboard still prints the CLI's fix (a command with flags).
  E5  more than one instruction line: a second sentence beside the footer.
  E6  s does not switch (start when stopped, stop when running).
  E7  a failure shows the CLI's text instead of "Could not start the
      dashboard. Run /doctor.", or its raw cause.
  E8  a status read that raises ends the TUI or shows the raw cause.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.nodes.node_onex_setup_effect import tabs_redesign_support as sup
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _Runner
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_dashboard import (
    _FIX,
    _URL,
    _Dashboard,
)

pytestmark = pytest.mark.unit


def _line(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"no line holds {needle!r}:\n{text}")


async def _text(size: tuple[int, int], running: bool, **kwargs: object) -> str:
    app = sup.app(_Dashboard(running=running, **kwargs))
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, "dashboard")
        return sup.screen_text(app)


@pytest.mark.parametrize("size", sup.SIZES)
async def test_title_row_has_the_state_and_pid(size: tuple[int, int]) -> None:
    """E1."""
    running = _line(await _text(size, True), "Local dashboard")
    assert "● Running · pid 35653" in running, running
    assert running.index("Local dashboard") < running.index("● Running")
    stopped = _line(await _text(size, False), "Local dashboard")
    assert "○ Stopped" in stopped, stopped


@pytest.mark.parametrize("size", sup.SIZES)
async def test_address_shows_only_while_running(size: tuple[int, int]) -> None:
    """E2."""
    running = await _text(size, True)
    assert _URL in running
    title_row = running.splitlines().index(_line(running, "Local dashboard"))
    assert _URL in running.splitlines()[title_row + 1], running
    assert _URL not in await _text(size, False)


@pytest.mark.parametrize("size", sup.SIZES)
async def test_buttons_follow_the_state(size: tuple[int, int]) -> None:
    """E3."""
    running = await _text(size, True)
    assert "Open in browser" in running
    assert "Stop" in _line(running, "Open in browser")
    assert "Start" not in running
    stopped = await _text(size, False)
    assert "Start" in stopped
    assert "Open in browser" not in stopped
    for text in (running, stopped):
        assert "Cancel" not in text, text


@pytest.mark.parametrize("size", sup.SIZES)
async def test_a_stopped_dashboard_does_not_print_the_clis_fix(
    size: tuple[int, int],
) -> None:
    """E4."""
    text = await _text(size, False)
    assert "onex setup" not in text
    assert _FIX[:20] not in text
    assert "Not running" not in text


@pytest.mark.parametrize("size", sup.SIZES)
async def test_the_only_instruction_is_the_footer_line(size: tuple[int, int]) -> None:
    """E5."""
    running = await _text(size, True)
    assert "enter open · s stop · esc close" in running
    assert "Press Enter" not in running
    assert "onex dashboard open" not in running
    stopped = await _text(size, False)
    assert "s start · esc close" in stopped


async def test_s_switches_and_x_still_stops() -> None:
    """E6."""
    run = _Dashboard(running=False)
    app = sup.app(run)
    async with app.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, app, "dashboard")
        await pilot.press("s")
        await sup.settle(pilot, 16)
        assert ("dashboard", "start", "--json") in run.calls
        assert "● Running" in sup.screen_text(app)
        await pilot.press("s")
        await sup.settle(pilot, 16)
        assert run.calls.count(("dashboard", "stop", "--json")) == 1
        assert "○ Stopped" in sup.screen_text(app)
        await pilot.press("s")
        await sup.settle(pilot, 16)
        await pilot.press("x")
        await sup.settle(pilot, 16)
        assert run.calls.count(("dashboard", "stop", "--json")) == 2


@pytest.mark.parametrize("size", sup.SIZES)
async def test_a_failed_start_says_it_in_plain_words(size: tuple[int, int]) -> None:
    """E7."""
    app = sup.app(_Dashboard(running=False, start_fails=True))
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, "dashboard")
        await pilot.press("s")
        await sup.settle(pilot, 20)
        text = sup.screen_text(app)
    assert "Could not start the dashboard. Run /doctor." in text, text
    assert "onex setup" not in text
    assert "did not work" not in text


class _Raising(_Runner):
    def __init__(self, error: str) -> None:
        super().__init__([])
        self._error = error

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        if argv[:1] == ("dashboard",):
            raise OSError(13, "Permission denied", self._error)
        return await super().capture(argv, stdin)


@pytest.mark.parametrize("size", sup.SIZES)
async def test_no_raw_error_on_screen_when_the_state_cannot_be_read(
    size: tuple[int, int], tmp_path: Path
) -> None:
    """AC9, E8."""
    app = sup.app(_Raising(str(sup.planted_path(tmp_path))))
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, "dashboard")
        await sup.settle(pilot, 15)
        text = sup.screen_text(app)
    sup.assert_no_raw(text, tmp_path)
    assert "Could not read the dashboard. Run /doctor." in text, text
