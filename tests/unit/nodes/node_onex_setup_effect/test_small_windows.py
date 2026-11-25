"""OMN-26620: windows smaller than the tabs need (real-terminal sweep at 60x20
and 120x36, 2026-11-25).

  S1  below 80x24 a tab cuts its parts off: Model drops Status, Delegation
      shows an empty order box and a cropped sentence, Metering loses the model
      names, Themes breaks words; nothing says why.
  S2  the notice that replaces a tab traps the user: Esc no longer closes it.
  P1  at 120x36 the open command menu and the welcome do not both fit: the
      screen scrolls, a stray scrollbar shows and the prompt leaves the screen.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _root,
    _Runner,
    _selected_rows,
    _type,
)

pytestmark = pytest.mark.timeout(60)


async def _status() -> ModelTuiStatus:
    return ModelTuiStatus(models=2, delegation="off")


def _app() -> OnexTui:
    return OnexTui(
        root=_root,
        runner=_Runner([], by_command=_selected_rows(connection=False)),
        load_status=_status,
    )


def _text(app: OnexTui) -> str:
    rows = [
        "".join(segment.text for segment in strip)
        for strip in app.screen._compositor.render_strips()
    ]
    return " ".join(" ".join(rows).split())


async def _until_open(pilot, app: OnexTui) -> None:  # type: ignore[no-untyped-def]
    """Until the tab is mounted (its size check ran), however loaded the host."""
    for _ in range(200):
        await pilot.pause()
        if len(app.screen_stack) > 1 and app.screen.query("#too-small"):
            break
    for _ in range(3):
        await pilot.pause()


@pytest.mark.parametrize(
    "command",
    ["/config", "/model", "/delegation", "/dashboard", "/metering", "/theme"],
)
@pytest.mark.parametrize(("size", "small"), [((60, 20), True), ((80, 24), False)])
async def test_a_tab_in_a_small_window_says_why(
    command: str, size: tuple[int, int], small: bool
) -> None:
    """S1."""
    app = _app()
    async with app.run_test(size=size) as pilot:
        await _type(pilot, command)
        await pilot.press("enter")
        await _until_open(pilot, app)
        text = _text(app)
    notice = f"This window is {size[0]}\u00d7{size[1]}"
    assert (notice in text) == small, text
    if small:
        assert "at least 80\u00d724" in text, text


async def test_esc_closes_a_tab_that_is_too_small() -> None:
    """S2."""
    app = _app()
    async with app.run_test(size=(60, 20)) as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        await _until_open(pilot, app)
        assert len(app.screen_stack) == 2
        await pilot.press("escape")
        for _ in range(4):
            await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_the_open_command_menu_fits_with_the_welcome() -> None:
    """P1."""
    app = _app()
    async with app.run_test(size=(120, 36)) as pilot:
        for _ in range(6):
            await pilot.pause()
        await pilot.press("/")
        for _ in range(6):
            await pilot.pause()
        main = app.screen
        assert main.query_one("#palette").display
        assert main.max_scroll_y == 0, main.max_scroll_y
        prompt = main.query_one("#prompt").region
        assert 0 <= prompt.y < 36, prompt
