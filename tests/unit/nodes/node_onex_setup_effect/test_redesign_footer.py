# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26620 redesign, footer hints: bold keys, muted words, middle-dot
separators, and a drop order declared per screen. Drawn by a Textual pilot at
80x24 and 140x40.

Failure modes:
  H1  the hint is one fixed string, so at 80 columns the status bar clips it.
  H2  a hint key is not bold, or its words are not muted, or the separator is
      not the design's.
  H3  the drop order is not the declared one, or it drops the quit key first.
  H4  a design whose drop order names a key the screen lacks loads anyway.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from rich.text import Text
from textual.widgets import Static

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    KeyFooter,
    ModelTuiStatus,
    OnexTui,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
    load_tui_design,
    tui_design,
)

pytestmark = pytest.mark.unit


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Last-Model", models=3, delegation="hook")


def _keys(text: Text) -> list[str]:
    """The keys of a rendered hint line: its bold runs."""
    return [text.plain[s.start : s.end] for s in text.spans if "bold" in str(s.style)]


def test_keys_are_bold_words_are_muted_and_dots_separate() -> None:
    """H2."""
    design = tui_design()
    line = KeyFooter().render("main", width=200)
    assert isinstance(line, Text)
    keys = design.footer.screens["main"].keys
    assert _keys(line) == [k.label for k in keys]
    assert line.plain.count(design.footer.separator) == len(keys) - 1
    muted = design.theme.muted
    for key in keys:
        start = line.plain.index(key.label) + len(key.label)
        words = line.plain[start : start + 1 + len(key.words)].strip()
        assert words == key.words
        assert any(
            muted in str(s.style) and s.start <= start + 1 < s.end for s in line.spans
        ), key


def test_the_footer_drops_keys_in_the_declared_order_and_keeps_quit() -> None:
    """H3."""
    design = tui_design()
    screen = design.footer.screens["main"]
    widths = {}
    for width in range(
        len(KeyFooter().render("main", 400).plain), len("ctrl+c quit") - 1, -1
    ):
        shown = _keys(KeyFooter().render("main", width))
        widths.setdefault(len(shown), shown)
    dropped: list[str] = []
    previous = [k.label for k in screen.keys]
    for count in sorted(widths, reverse=True):
        gone = [k for k in previous if k not in widths[count]]
        dropped += gone
        previous = widths[count]
    assert dropped == [k for k in screen.drop_order if k in dropped]
    assert "ctrl+c" not in dropped[:1]
    assert _keys(KeyFooter().render("main", len("ctrl+c quit"))) == ["ctrl+c"]
    assert KeyFooter().render("main", 3).plain == ""


@pytest.mark.parametrize(("width", "height"), [(80, 24), (140, 40)])
async def test_the_status_bar_hint_is_never_clipped(width: int, height: int) -> None:
    """H1: every key the bar shows is whole, and the bar is one line."""
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(width, height)) as pilot:
        for _ in range(4):
            await pilot.pause()
        bar = app.query_one("#status", Static).render()
        plain = bar.plain if isinstance(bar, Text) else str(bar)
        assert "\n" not in plain
        assert len(plain) <= width - 2, (len(plain), plain)
        hint = [k.label for k in tui_design().footer.screens["main"].keys]
        for key in hint:
            if key in plain:  # a key that shows has its words
                words = next(
                    k.words
                    for k in tui_design().footer.screens["main"].keys
                    if k.label == key
                )
                assert f"{key} {words}" in plain, (key, plain)
        assert "ctrl+c quit" in plain
        if width >= 140:
            assert all(key in plain for key in hint)


def _changed(tmp_path: Path, change: dict[str, Any]) -> Path:
    data = yaml.safe_load(
        (Path(cli_tui.__file__).parent / "tui_design.yaml").read_text()
    )
    for dotted, value in change.items():
        target = data
        *parents, last = dotted.split(".")
        for part in parents:
            target = target[int(part)] if part.isdigit() else target[part]
        target[last] = value
    path = tmp_path / "design.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    return path


def test_a_drop_order_naming_a_missing_key_is_refused(tmp_path: Path) -> None:
    """H4."""
    bad = _changed(tmp_path, {"footer.screens.main.drop_order": ["ctrl+z"]})
    with pytest.raises(ValidationError, match="drop order"):
        load_tui_design(bad)
