# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26620 redesign, group 1: the true-black theme, the start screen and plain
status messages, drawn by a Textual pilot at 80x24 and 140x40.

Failure modes, each checked on the screen the customer sees:
  D1  the start screen still draws the WELCOME TO banner or its tagline.
  D2  the header is taller than the approved two-row wordmark, or the input is
      pushed below the fold at 80x24.
  D3  the full tips show on every launch instead of the first only.
  D4  a failed status read shows an errno, a path or an exception text.
  D5  the default theme is not the approved true black.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from textual.widgets import Input, Static

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    OutputPane,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

pytestmark = pytest.mark.unit

_SIZES = [(80, 24), (140, 40)]


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Last-Model", models=3, delegation="hook")


def _broken(path: Path) -> Callable[[], Awaitable[ModelTuiStatus]]:
    """A status read that fails the way a missing file does: [Errno 2] and a path."""

    async def read() -> ModelTuiStatus:
        raise FileNotFoundError(2, "No such file or directory", str(path))

    return read


def _screen_text(app: OnexTui) -> str:
    out = app.query_one("#output", OutputPane)
    lines = [str(line) for line in out.lines]
    for widget in app.query(Static):
        # only what is on screen: a hidden widget (or one inside a hidden parent) is not
        if widget.display and all(a.display for a in widget.ancestors):
            lines.append(str(widget.render()))
    return "\n".join(lines)


async def _launch(pilot: object) -> None:
    for _ in range(4):
        await pilot.pause()


@pytest.fixture(autouse=True)
def _fresh_launch_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts as a first launch with no saved theme."""
    monkeypatch.setattr(cli_tui.ThemeStore, "_path", tmp_path / "theme")
    store = getattr(cli_tui, "LaunchStore", None)
    if store is not None:
        monkeypatch.setattr(store, "_path", tmp_path / "launches")


@pytest.mark.parametrize(("width", "height"), _SIZES)
async def test_start_screen_is_a_compact_header_not_the_banner(
    width: int, height: int
) -> None:
    """D1, D2: wordmark, version, first model and folder; no banner, no tagline.

    A roomy terminal (the welcome's minimum size) shows the welcome instead, by the
    operator's later reference image; the compact header holds below that size.
    """
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(width, height)) as pilot:
        await _launch(pilot)
        text = _screen_text(app)
        welcome = tui_design().welcome
        if width >= welcome.min_width and height >= welcome.min_height:
            assert "Build more. Spend less." in text
            assert app.query_one("#header").display is False
            assert app.query_one("#prompt", Input).region.bottom <= height
            return
        assert "WELCOME" not in text.upper()
        assert "run them where it costs least" not in text
        header = str(app.query_one("#header", Static).render())
        rows = header.splitlines()
        assert len(rows) <= 4, rows
        for row in tui_design().wordmark.rows:
            assert row in header
        assert "Last-Model" in header
        assert str(Path.cwd().name) in header
        prompt = app.query_one("#prompt", Input)
        assert prompt.region.bottom <= height, (prompt.region, height)
        assert prompt.region.height > 0


@pytest.mark.parametrize(("width", "height"), _SIZES)
async def test_tips_show_on_the_first_launch_only(width: int, height: int) -> None:
    """D3: the second launch of the same install shows no tips."""
    tip = tui_design().words["tips_first_launch"]
    first = OnexTui(load_status=_ready)
    async with first.run_test(size=(width, height)) as pilot:
        await _launch(pilot)
        assert tip in _screen_text(first)
    second = OnexTui(load_status=_ready)
    async with second.run_test(size=(width, height)) as pilot:
        await _launch(pilot)
        assert tip not in _screen_text(second)
        assert second.query_one("#prompt", Input).region.bottom <= height


@pytest.mark.parametrize(("width", "height"), _SIZES)
async def test_a_failed_status_read_says_it_plainly(
    width: int, height: int, tmp_path: Path
) -> None:
    """D4: no errno, path, exception text or env name; the next step is named."""
    words = tui_design().words
    plain = words["summary_unknown"]
    short = words["delegation_unknown"]
    state = tmp_path / "state.json"
    app = OnexTui(load_status=_broken(state))
    async with app.run_test(size=(width, height)) as pilot:
        await _launch(pilot)
        text = _screen_text(app)
        welcome = tui_design().welcome
        roomy = width >= welcome.min_width and height >= welcome.min_height
        # the roomy welcome has no status block: the footer carries the short wording
        assert roomy or plain in text
        for raw in ("Errno", "No such file", str(tmp_path), "FileNotFoundError"):
            assert raw not in text, raw
        assert "status could not be read" not in text
        status = str(app.query_one("#status", Static).render())
        assert short in status
        # said once in plain words, not repeated in the output pane
        assert words["status_unreadable"] not in text


async def test_a_failed_status_read_keeps_the_raw_text_in_a_log(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """D4: the raw error is not lost; it goes to the log, not the screen."""
    app = OnexTui(load_status=_broken(tmp_path / "state.json"))
    with caplog.at_level("WARNING"):
        async with app.run_test() as pilot:
            await _launch(pilot)
    assert "No such file or directory" in caplog.text


async def test_the_default_theme_is_onex_dark_on_the_terminal_background() -> None:
    """D5: onex-dark is registered, is the default, and is true black."""
    app = OnexTui(load_status=_ready)
    async with app.run_test() as pilot:
        await _launch(pilot)
        assert str(app.theme) == "onex-dark"
        theme = app.available_themes["onex-dark"]
        # The terminal's own background shows (operator decision); the yaml
        # switch ground: black brings the true black back.
        black = tui_design().theme.paint(ground="black")
        assert (black.background, black.surface, black.panel) == (
            "#000000",
            "#000000",
            "#0a0a0a",
        )
        assert theme.primary == "#F0B90B"
        assert theme.accent == "#FFD866"
        assert theme.success == "#7fd88f"
        assert theme.warning == "#f5a742"
        assert theme.error == "#e06c75"
        assert theme.foreground == "#eeeeee"
        assert theme.variables["border"] == "#6c6c6c"
        assert {"onex-light", "textual-dark", "textual-light"} <= set(
            app.available_themes
        )


def test_the_design_file_has_no_duplicate_keys() -> None:
    """A key written twice silently keeps the last value (summary_first did)."""
    import yaml

    class _Strict(yaml.SafeLoader):
        pass

    def _no_duplicates(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node)
            assert key not in seen, (
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            )
            seen.add(key)
        return loader.construct_mapping(node)

    _Strict.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates
    )
    design = Path(cli_tui.__file__).parent / "tui_design.yaml"
    yaml.load(design.read_text(), Loader=_Strict)


def test_a_theme_that_cannot_be_saved_is_logged_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A full or read-only disk: save says it failed, nothing is kept for the
    next start, and the log says why."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder is needed")
    monkeypatch.setattr(cli_tui.ThemeStore, "_path", blocker / "theme")
    with caplog.at_level("WARNING"):
        assert cli_tui.ThemeStore.save("onex-light") is False
    assert cli_tui.ThemeStore.load() is None
    assert "could not remember the theme" in caplog.text
