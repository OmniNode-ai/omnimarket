# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26620 mockups-v2: the prompt row is a marker and a borderless input, and
a bare ? on an empty prompt shows help. Drawn by a Textual pilot at 80x24 and
140x40.

Failure modes:
  R1  the prompt is a bordered box with no marker, and says ONEX in capitals.
  R2  ? does nothing on an empty prompt, or eats the question mark of a typed task.
  R3  the footer does not list the ? key the mockup shows.
"""

from __future__ import annotations

import pytest
from textual.widgets import Input, Static

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    KeyFooter,
    ModelTuiStatus,
    OnexTui,
    OutputPane,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

pytestmark = pytest.mark.unit

_SIZES = [(80, 24), (140, 40)]


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Last-Model", models=3, delegation="hook")


async def _settle(pilot: object) -> None:
    for _ in range(4):
        await pilot.pause()  # type: ignore[attr-defined]


def _log(app: OnexTui) -> str:
    return " ".join(
        str(line.text) for line in app.query_one("#output", OutputPane).lines
    )


@pytest.mark.parametrize(("width", "height"), _SIZES)
async def test_the_prompt_is_a_marker_and_a_borderless_input(
    width: int, height: int
) -> None:
    """R1."""
    design = tui_design()
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(width, height)) as pilot:
        await _settle(pilot)
        prompt = app.query_one("#prompt", Input)
        mark = app.query_one("#promptmark", Static)
        assert str(mark.render()).strip() == design.glyphs.prompt
        assert mark.region.y == prompt.region.y  # one row
        assert mark.region.right <= prompt.region.x
        assert prompt.region.height == 1
        assert prompt.styles.border.top[0] == ""  # no box
        assert prompt.placeholder == design.prompt_placeholder
        assert "onex" in prompt.placeholder
        assert "ONEX" not in prompt.placeholder
        assert prompt.region.bottom <= height


async def test_a_bare_question_mark_shows_help() -> None:
    """R2: on an empty prompt ? is the help key."""
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(pilot)
        await pilot.press("question_mark")
        await _settle(pilot)
        assert " ".join(tui_design().help.split())[:40] in _log(app)
        assert app.query_one("#prompt", Input).value == ""


async def test_a_question_mark_inside_a_task_is_typed_not_taken() -> None:
    """R2: once there is text, ? is part of it."""
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(pilot)
        await pilot.press("w", "h", "y", "question_mark")
        await _settle(pilot)
        assert app.query_one("#prompt", Input).value == "why?"
        assert " ".join(tui_design().help.split())[:40] not in _log(app)


def test_the_footer_lists_the_help_key_in_the_mockup_order() -> None:
    """R3: / commands · ↑↓ history · ctrl+p palette · ? help · ctrl+c quit."""
    line = KeyFooter().render("main", 200).plain
    order = ["/ commands", "↑↓ history", "ctrl+p palette", "? help", "ctrl+c quit"]
    assert [line.index(x) for x in order] == sorted(line.index(x) for x in order)
