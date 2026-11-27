# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC5 and AC3 (OMN-26620 review, 2026-11-27): the text is drawn in the theme on
screen, not in onex dark.

Failure modes:
  TC1  after /theme picks onex light, the status bar's words keep the onex dark
       colours (light grey on the cream background).
  TC2  a task card's bar keeps the onex dark colour after the theme changes.
  TC3  a Textual built-in theme leaves the text in onex dark colours.
  TC4  text drawn outside a running TUI stops using the default theme.
"""

from __future__ import annotations

import pytest
from rich.style import Style
from rich.text import Text
from textual.color import Color
from textual.pilot import Pilot
from textual.widgets import Static

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    TaskCard,
    _style,
    apply_theme,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

pytestmark = pytest.mark.unit


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Last-Model", models=3, delegation="hook")


def _colours(widget: Static) -> set[str]:
    """Every colour the widget's text is drawn in, as #rrggbb."""
    content = widget.content
    assert isinstance(content, Text), type(content)
    found: set[str] = set()
    for span in content.spans:
        style = Style.parse(span.style) if isinstance(span.style, str) else span.style
        if style.color is not None and style.color.triplet is not None:
            found.add(style.color.triplet.hex.lower())
    return found


def _hex(token: str, theme: str) -> str:
    design = tui_design()
    tokens = design.theme if theme == design.theme.name else design.themes[theme]
    return str(getattr(tokens, token)).lower()


async def _settle(pilot: Pilot[int]) -> None:
    for _ in range(4):
        await pilot.pause()


async def test_the_status_bar_and_a_card_follow_onex_light() -> None:
    """TC1, TC2."""
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(pilot)
        status = app.query_one("#status", Static)
        assert _hex("success", "onex-dark") in _colours(status)  # the control
        await apply_theme(app, "onex-light")
        await _settle(pilot)
        shown = _colours(status)
        dark_only = {
            _hex(token, "onex-dark")
            for token in ("success", "muted", "foreground", "secondary")
        }
        assert not shown & dark_only, shown
        assert _hex("success", "onex-light") in shown, shown
        card = TaskCard({"state": "done", "model": "m", "task": "t"})
        assert card.bar_colour().lower() == _hex("success", "onex-light")


async def test_a_built_in_theme_colours_the_text() -> None:
    """TC3: textual-light's own success and foreground, not onex dark's."""
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(pilot)
        await apply_theme(app, "textual-light")
        await _settle(pilot)
        theme = app.available_themes["textual-light"]
        success = Color.parse(str(theme.success)).hex.lower()
        assert _style("success").lower() == success
        assert _style("foreground").lower() != _hex("foreground", "onex-dark")
        assert success in _colours(app.query_one("#status", Static))


def test_text_outside_the_tui_keeps_the_default_theme() -> None:
    """TC4."""
    assert _style("success", "bold") == f"bold {tui_design().theme.success}"
