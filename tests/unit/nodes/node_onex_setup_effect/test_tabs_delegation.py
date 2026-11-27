# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation tab, mockup page 6 (OMN-26620 AC12).

Failure modes this suite is written against (enumerated before the code):
  D1  Claude Code's switch does not show the state: ON when on, OFF when off
      (OMN-26688, operator 2026-11-26: "i want toogle button").
  D2  the one-line explanation is missing or is an old sentence.
  D3  an agent has more than one control, or there is a Save button or the
      old Turn on / Turn off words (operator 2026-11-26: "one button").
  D4  the Hook line shows when nothing is wrong, or does not show when
      delegation is on and Claude Code lacks the hook.
  D5  (retired 2026-11-26: the tab has no models list; test_delegation_agents
      A2 checks it stays off.)
  D6  Space or a click does not switch at once, or waits for a Save.
  D8  a switch that failed leaves the toggle showing the state it did not
      reach.
  D7  a failed save shows the raw cause (errno, path, exception, env name).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.cli_toggle import ToggleSwitch
from tests.unit.nodes.node_onex_setup_effect import tabs_redesign_support as sup
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _Runner

pytestmark = pytest.mark.unit


def _line(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"no line holds {needle!r}:\n{text}")


async def _text(size: tuple[int, int], **kwargs: object) -> str:
    app = sup.app(**kwargs)
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        return sup.screen_text(app)


@pytest.mark.parametrize("size", sup.SIZES)
async def test_the_toggle_shows_the_state(size: tuple[int, int]) -> None:
    """D1."""
    on = _line(await _text(size), "Claude Code")  # its switch is on its line
    assert "ON" in on, on
    assert "OFF" not in on, on
    off = _line(
        await _text(size, delegation="off", hooks={"prompt_hook": "missing"}),
        "Claude Code",
    )
    assert "OFF" in off, off


@pytest.mark.parametrize("size", sup.SIZES)
async def test_one_line_explanation(size: tuple[int, int]) -> None:
    """D2."""
    text = await _text(size)
    assert "Your agents. Your models. Your choice." in text
    for old in ("Delegation runs through one hook", "Claude Code is the fallback"):
        assert old not in text, (old, text)


@pytest.mark.parametrize("size", sup.SIZES)
async def test_one_toggle_and_no_save(size: tuple[int, int]) -> None:
    """D3."""
    app = sup.app()
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        switches = [switch.name for switch in app.screen.query(ToggleSwitch)]
        claude = app.screen.query_one("#dturn", ToggleSwitch).name
        buttons = [button.id for button in app.screen.query("#deleg Button")]
        saves = list(app.screen.query("#dsave"))
        text = sup.screen_text(app)
    assert len(switches) == len(set(switches)) == 20, switches
    assert claude == "claude_code", claude
    assert buttons == ["dcancel"], buttons
    assert saves == [], saves
    for old in ("Turn on", "Turn off", "ctrl+s", "press Save"):
        assert old not in text, (old, text)


@pytest.mark.parametrize("size", sup.SIZES)
async def test_hook_line_only_when_something_is_wrong(size: tuple[int, int]) -> None:
    """D4."""
    on = await _text(size)
    assert "Hook" not in on, on
    off = await _text(size, delegation="off", hooks={"prompt_hook": "missing"})
    assert "Hook" not in off, off
    broken = _line(await _text(size, hooks={"prompt_hook": "missing"}), "Hook")
    assert "! not installed" in broken, broken


@pytest.mark.parametrize("how", ["space", "click"])
async def test_space_or_a_click_switches_at_once(how: str) -> None:
    """D6: the flip sends `delegation off` with no Save pressed."""
    run = _Runner([])
    app = sup.app(run)
    async with app.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        if how == "space":
            await pilot.press("space")
        else:
            await pilot.click("#dturn")
        await sup.settle(pilot, 25)
        text = sup.screen_text(app)
    assert ("delegation", "off", "--json") in run.calls, run.calls
    assert "press Save to apply" not in text, text


async def test_a_failed_switch_shows_the_state_it_stayed_in() -> None:
    """D8."""
    run = _Runner([], code=1, errors="Error: could not change the settings")
    app = sup.app(run)
    async with app.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        await pilot.press("space")
        await sup.settle(pilot, 25)
        toggle = app.screen.query_one("#dturn", ToggleSwitch)
        label = toggle.drawn.plain
        on = toggle.has_class("-on")
    assert "ON" in label, label
    assert "OFF" not in label, label
    assert on


@pytest.mark.parametrize("size", sup.SIZES)
async def test_no_raw_error_on_screen_when_saving_fails(
    size: tuple[int, int], tmp_path: Path
) -> None:
    """AC9, D7."""
    run = _Runner([], code=1, errors=f"Error: {sup.raw_text(tmp_path)}")
    app = sup.app(run)
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        await pilot.press("space")  # switches at once (D6)
        await sup.settle(pilot, 25)
        text = sup.screen_text(app)
    sup.assert_no_raw(text, tmp_path)
    assert "Could not change delegation. Run /doctor." in text, text
