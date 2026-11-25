# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26620 redesign, group 3: the palette lists the nine customer commands
with icons, and ctrl+p opens the same list plus Themes with a live preview.
Drawn by a Textual pilot at 80x24 and 140x40.

Failure modes:
  P1  the / palette shows a row without its short hint or its icon.
  P2  the word "every command" is still on the screen.
  P3  ctrl+p opens Textual's own system commands (Screenshot, Maximize, Keys)
      instead of the customer commands.
  P4  an icon shows a Nerd Font glyph while the switch is off, or has no fallback.
  P5  Themes has no live preview: moving through the themes leaves the theme alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.command import CommandList
from textual.widgets import Input, OptionList, Static

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

pytestmark = pytest.mark.unit

_SIZES = [(80, 24), (140, 40)]


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Last-Model", models=3, delegation="hook")


@pytest.fixture(autouse=True)
def _fresh_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_tui.ThemeStore, "_value", None)
    monkeypatch.setattr(cli_tui.ThemeStore, "_path", tmp_path / "theme")
    monkeypatch.setattr(cli_tui.LaunchStore, "_path", tmp_path / "launches")


async def _settle(pilot: object) -> None:
    for _ in range(4):
        await pilot.pause()  # type: ignore[attr-defined]


def _rows(palette: OptionList) -> list[str]:
    return [
        str(palette.get_option_at_index(i).prompt) for i in range(palette.option_count)
    ]


@pytest.mark.parametrize(("width", "height"), _SIZES)
async def test_slash_lists_the_nine_commands_with_icons_and_hints(
    width: int, height: int
) -> None:
    """P1, P2: nine rows in design order, each with its hint; the icon ones carry it."""
    design = tui_design()
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(width, height)) as pilot:
        await _settle(pilot)
        await pilot.press("slash")
        await _settle(pilot)
        palette = app.query_one("#palette", OptionList)
        assert palette.display
        rows = _rows(palette)
        assert len(rows) == len(design.customer_commands) == 10
        for row, path in zip(rows, design.customer_commands, strict=True):
            name = path[0]
            assert f"/{name}" in row, (name, row)
            assert design.palette.hints[name] in row, (name, row)
            glyph = cli_tui.icon(f"cmd_{name}")
            if name in design.palette.icon_commands:
                assert glyph, name
                assert glyph in row, (name, row)
        shown = "\n".join(str(w.render()) for w in app.query(Static))
        assert "every command" not in (shown + "\n".join(rows)).lower()
        assert palette.region.bottom <= height


async def test_ctrl_p_lists_the_customer_commands_and_themes_only() -> None:
    """P3: Textual's own Screenshot, Maximize and Keys commands are not offered."""
    design = tui_design()
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(pilot)
        await pilot.press("ctrl+p")
        await _settle(pilot)
        listed = app.screen.query_one(CommandList)
        titles = [
            str(listed.get_option_at_index(i).prompt)
            for i in range(listed.option_count)
        ]
        text = "\n".join(titles)
        for path in design.customer_commands:
            assert f"/{path[0]}" in text, path
        assert design.palette.themes_title in text
        for own in ("Screenshot", "Maximize", "Keys", "Minimize"):
            assert own not in text, own


async def test_ctrl_p_runs_a_command_like_typing_it() -> None:
    """Choosing /help from ctrl+p writes the same help the typed command writes."""
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(pilot)
        await pilot.press("ctrl+p")
        await _settle(pilot)
        await pilot.press(*"/help")
        await _settle(pilot)
        await pilot.press("enter")
        await _settle(pilot)
        log = "\n".join(str(line) for line in app.query_one("#output").lines)
        assert tui_design().help.split(".")[0] in " ".join(log.split())


async def test_themes_previews_each_theme_as_it_is_highlighted() -> None:
    """P5: moving through the Themes list changes the theme before Enter."""
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(pilot)
        start = str(app.theme)
        await pilot.press("ctrl+p")
        await _settle(pilot)
        await pilot.press(*tui_design().palette.themes_title.lower(), "enter")
        await _settle(pilot)
        seen = {start}
        for _ in range(4):
            await pilot.press("down")
            await _settle(pilot)
            seen.add(str(app.theme))
        assert len(seen) > 1, seen
        await pilot.press("escape")
        await _settle(pilot)
        assert str(app.theme) == start  # leaving without a choice restores it
        assert cli_tui.ThemeStore.load() is None  # and nothing was remembered


async def test_enter_on_a_theme_keeps_it_and_remembers_it() -> None:
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(pilot)
        await pilot.press("ctrl+p")
        await _settle(pilot)
        await pilot.press(*tui_design().palette.themes_title.lower(), "enter")
        await _settle(pilot)
        await pilot.press("down", "enter")
        await _settle(pilot)
        chosen = str(app.theme)
        assert chosen != "onex-dark"
        assert cli_tui.ThemeStore.load() == chosen


def test_icons_default_to_unicode_and_switch_to_nerd_and_ascii(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P4: each icon has a Unicode default, a Nerd Font glyph and an ASCII form."""
    design = tui_design()
    names = [f"cmd_{name}" for name in design.palette.icon_commands]
    assert names
    for name in names:
        spec = design.icons.glyphs[name]
        for form in (spec.fallback, spec.nerd, spec.ascii):
            assert form, name
        assert cli_tui.icon(name) == spec.fallback  # default: Unicode, not Nerd
        assert spec.ascii.isascii()
    for mode, field in (("nerd", "nerd"), ("ascii", "ascii")):
        switched = design.model_copy(
            update={"icons": design.icons.model_copy(update={"mode": mode})}
        )
        monkeypatch.setattr(cli_tui, "tui_design", lambda s=switched: s)
        for name in names:
            assert cli_tui.icon(name) == getattr(design.icons.glyphs[name], field)


async def test_the_prompt_stays_in_view_with_the_palette_open() -> None:
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(pilot)
        await pilot.press("slash")
        await _settle(pilot)
        prompt = app.query_one("#prompt", Input)
        assert prompt.region.bottom <= 24
