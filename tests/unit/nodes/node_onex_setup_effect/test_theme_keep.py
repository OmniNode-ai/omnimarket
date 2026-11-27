"""OMN-26620: Enter on /theme keeps the highlighted theme.

Failure mode (found by the type checker 2026-11-25): the "kept" message passed
``name=`` to a function whose own first parameter is ``name``, so Enter raised
instead of keeping the theme, and no test pressed Enter.
"""

from __future__ import annotations

from omnimarket.nodes.node_onex_setup_effect.cli_tui import OnexTui, ThemeStore

from .test_metering_theme_screens import _REPORT, _Meter, _ready


async def test_enter_keeps_the_highlighted_theme_and_says_so() -> None:
    app = OnexTui(load_status=_ready, runner=_Meter(_REPORT))
    async with app.run_test(size=(140, 40)) as pilot:
        for _ in range(3):
            await pilot.pause()
        before = str(app.theme)
        await pilot.press(*"/theme", "enter")
        for _ in range(8):
            await pilot.pause(0.1)
        await pilot.press("down")
        for _ in range(3):
            await pilot.pause()
        chosen = str(app.theme)
        assert chosen != before
        await pilot.press("enter")
        for _ in range(4):
            await pilot.pause()
        note = str(app.screen.query_one("#tnote").render())
        assert f"{chosen} kept." in note, note
        await pilot.press("escape")
        for _ in range(4):
            await pilot.pause()
        assert str(app.theme) == chosen  # Esc after Enter does not undo it
        assert ThemeStore._path.read_text() == chosen  # kept on disk, not in memory
