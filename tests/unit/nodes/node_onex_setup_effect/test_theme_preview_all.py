"""OMN-26620: the Themes tab previews every theme it lists (real-terminal
sweep, 2026-11-25: arrowing onto ansi-dark ended onex with a traceback from
the preview's colour swatches).

  T1  a listed theme whose colours Rich cannot draw as given crashes onex.
  T2  every swatch shows a colour, never a blank.
"""

from __future__ import annotations

import pytest
from textual.widgets import OptionList, Static

from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _Runner,
    _selected_rows,
    _type,
)

pytestmark = pytest.mark.timeout(120)


async def test_every_listed_theme_previews() -> None:
    """T1, T2."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(120, 40)) as pilot:
        await _type(pilot, "/theme")
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        themes = app.screen.query_one("#tlist", OptionList)
        names = [themes.get_option_at_index(i).id for i in range(themes.option_count)]
        assert "ansi-dark" in names, names
        shown: dict[str, str] = {}
        for index, name in enumerate(names):
            themes.highlighted = index
            for _ in range(3):
                await pilot.pause()
            preview = app.screen.query_one("#tpreview", Static)
            shown[str(name)] = " ".join(str(preview.render()).split())
            assert app.is_running, name
        await pilot.press("escape")
    for name, text in shown.items():
        assert text.startswith(name), (name, text)
        assert "████████" in text, (name, text)


@pytest.mark.parametrize("theme", ["ansi-dark", "ansi-light"])
async def test_a_kept_ansi_theme_draws_every_screen(theme: str) -> None:
    """T3: a theme kept with Enter colours every screen after it."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(120, 40)) as pilot:
        app.theme = theme
        for command in (
            "/help",
            "/config",
            "/model",
            "/delegation",
            "/dashboard",
            "/metering",
            "/theme",
        ):
            await _type(pilot, command)
            await pilot.press("enter")
            for _ in range(6):
                await pilot.pause()
            assert app.is_running, command
            app.screen._compositor.render_strips()
            if len(app.screen_stack) > 1:
                await pilot.press("escape")
                for _ in range(3):
                    await pilot.pause()
        assert app.theme == theme
