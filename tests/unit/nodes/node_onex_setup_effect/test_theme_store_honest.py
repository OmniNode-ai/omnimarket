# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC5 (OMN-26620 review, 2026-11-27): the kept theme is read from the file, a theme that could not be kept is said plainly, and a saved theme that is gone opens the default.

Failure modes:
- TS1: a saved theme name that is no longer registered stops the TUI from starting (InvalidThemeError)
- TS2: save() reports success when the file was never written, so the next start is promised a theme it will not get
- TS3: the next start reads a copy held in memory, not the file, so a restore test passes with nothing on disk
- TS4: /theme says "<theme> kept." when the choice could not be written
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from textual.pilot import Pilot

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import OnexTui, ThemeStore
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

from .test_metering_theme_screens import _REPORT, _Meter, _ready

pytestmark = pytest.mark.unit


def _blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file where the theme's folder must go: the write fails as on a read-only disk."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder is needed")
    monkeypatch.setattr(ThemeStore, "_path", blocker / "theme")


async def _settle(pilot: Pilot[int], times: int = 4) -> None:
    for _ in range(times):
        await pilot.pause(0.1)


async def test_a_saved_theme_that_is_gone_opens_the_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TS1."""
    ThemeStore._path.write_text("a-theme-nobody-registers")
    with caplog.at_level(logging.WARNING):
        app = OnexTui(load_status=_ready)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert str(app.theme) == tui_design().theme.name
    assert "a-theme-nobody-registers" in caplog.text


def test_a_failed_write_is_reported_and_nothing_is_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TS2, TS3."""
    _blocked(tmp_path, monkeypatch)
    assert ThemeStore.save("onex-light") is False
    assert ThemeStore.load() is None


async def test_the_next_start_reads_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TS3: a theme that was not written is not restored."""
    _blocked(tmp_path, monkeypatch)
    ThemeStore.save("onex-light")
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert str(app.theme) == tui_design().theme.name


async def test_theme_says_plainly_when_the_choice_was_not_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TS4."""
    _blocked(tmp_path, monkeypatch)
    app = OnexTui(load_status=_ready, runner=_Meter(_REPORT))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(pilot)
        await pilot.press(*"/theme", "enter")
        await _settle(pilot, 8)
        await pilot.press("down")
        await _settle(pilot)
        chosen = str(app.theme)
        await pilot.press("enter")
        await _settle(pilot)
        note = str(app.screen.query_one("#tnote").render())
        assert note == cli_tui._say("themes_not_kept", theme=chosen), note
        assert str(app.theme) == chosen  # it stays on for this run
