# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation tab, mockup page 6 (OMN-26620 AC12).

Failure modes this suite is written against (enumerated before the code):
  D1  the title has no state beside it, or the state is not a dot and a word.
  D2  the one-line explanation is missing or is the old hook sentence.
  D3  there is no Turn on / Turn off button, or it names the wrong action.
  D4  the Hook line does not say whether Claude Code has the hook.
  D5  the order list is missing, numbered wrongly, or not in the order the
      models are tried, or a status is not a plain word.
  D6  the button and Space do not switch the same pending state.
  D7  a failed save shows the raw cause (errno, path, exception, env name).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.nodes.node_onex_setup_effect import tabs_redesign_support as sup
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _Runner

pytestmark = pytest.mark.unit


def _line(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"no line holds {needle!r}:\n{text}")


async def _text(size: tuple[int, int], **kwargs: object) -> str:
    app = sup.app(**kwargs)  # type: ignore[arg-type]
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        return sup.screen_text(app)


@pytest.mark.parametrize("size", sup.SIZES)
async def test_title_has_the_state_beside_it(size: tuple[int, int]) -> None:
    """D1."""
    on = _line(await _text(size), "Claude Code delegation")
    assert "● ON" in on, on
    assert on.index("Claude Code delegation") < on.index("● ON")
    off = _line(
        await _text(size, delegation="off", hooks={"prompt_hook": "missing"}),
        "Claude Code delegation",
    )
    assert "○ OFF" in off, off


@pytest.mark.parametrize("size", sup.SIZES)
async def test_one_line_explanation(size: tuple[int, int]) -> None:
    """D2."""
    text = await _text(size)
    assert "Prompts go to your models first. Claude Code is the fallback." in text
    assert "Delegation runs through one hook" not in text


@pytest.mark.parametrize("size", sup.SIZES)
async def test_turn_button_names_the_action(size: tuple[int, int]) -> None:
    """D3."""
    assert "Turn off" in await _text(size)
    text = await _text(size, delegation="off", hooks={"prompt_hook": "missing"})
    assert "Turn on" in text
    assert "Turn off" not in text


@pytest.mark.parametrize("size", sup.SIZES)
async def test_hook_line(size: tuple[int, int]) -> None:
    """D4."""
    installed = _line(await _text(size), "Hook")
    assert "✓ installed in Claude Code" in installed, installed
    missing = _line(
        await _text(size, delegation="off", hooks={"prompt_hook": "missing"}), "Hook"
    )
    assert "! not installed" in missing, missing


@pytest.mark.parametrize("size", sup.SIZES)
async def test_models_are_listed_in_the_order_they_are_tried(
    size: tuple[int, int],
) -> None:
    """D5."""
    text = await _text(size)
    assert "Tried in this order" in text
    rows = [_line(text, "1 qwen3"), _line(text, "2 gemini"), _line(text, "3 glm")]
    assert "qwen3-27b" in rows[0], rows[0]
    assert "✓ ready" in rows[0], rows[0]
    assert "gemini-flash" in rows[1], rows[1]
    assert "glm-5" in rows[2], rows[2]
    assert "! needs a key" in rows[2], rows[2]
    for old in ("READY", "UNAVAILABLE", "Your selected models"):
        assert old not in text, (old, text)


@pytest.mark.parametrize("how", ["space", "button"])
async def test_the_button_and_space_switch_the_same_pending_state(how: str) -> None:
    """D6: nothing is sent until Save; the screen says so."""
    app = sup.app()
    async with app.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        if how == "space":
            await pilot.press("space")
        else:
            app.screen.query_one("#dturn").press()
        await sup.settle(pilot)
        text = sup.screen_text(app)
    assert "Turn on" in text, text
    state = _line(text, "Claude Code delegation")
    assert "○ OFF" in state, state
    assert "press Save to apply" in state, state


@pytest.mark.parametrize("size", sup.SIZES)
async def test_no_raw_error_on_screen_when_saving_fails(
    size: tuple[int, int], tmp_path: Path
) -> None:
    """AC9, D7."""
    run = _Runner([], code=1, errors=f"Error: {sup.raw_text(tmp_path)}")
    app = sup.app(run)
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        await pilot.press("space")
        await pilot.press("ctrl+s")
        await sup.settle(pilot, 25)
        text = sup.screen_text(app)
    sup.assert_no_raw(text, tmp_path)
    assert "Could not change delegation. Run /doctor." in text, text
