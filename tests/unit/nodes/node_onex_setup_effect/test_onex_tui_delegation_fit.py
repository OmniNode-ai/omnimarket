# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""/delegation keeps every state word and line in sight at 80x24 (OMN-23413).

Seen in review 2026-11-15 on drawn text (render_lines), which the other tests
read only at 120 columns and wider. Failure modes:

  DF1  a hook row shown because the hook is missing loses its state word
       (OMN-26688: the row shows only when something is wrong).
  DF3  the key line, or a Save outcome longer than one line, sits under the
       docked buttons.
"""

from __future__ import annotations

import json

import pytest
from textual.pilot import Pilot
from textual.widget import Widget

from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _root,
    _Runner,
    _selected_rows,
    _type,
)

pytestmark = pytest.mark.unit

_SIZES = [(80, 24), (160, 46)]


def _shown(widget: Widget) -> str:
    """What the screen shows where ``widget`` is: clipped by its parents and
    overdrawn by anything on top, as the user reads it."""
    compositor = widget.screen._compositor
    geometry = compositor.find_widget(widget)
    area = geometry.region.intersection(geometry.clip)
    strips = compositor.render_strips()
    lines = [
        strips[y].crop(area.x, area.x + area.width).text
        for y in range(area.y, area.y + area.height)
    ]
    return " ".join(" ".join(lines).split())


def _app(
    hooks: dict[str, str],
    rows: dict[tuple[str, ...], str] | None = None,
    delegation: str = "off",
) -> OnexTui:
    runner = _Runner([], by_command=rows or _selected_rows(connection=False))

    async def status() -> ModelTuiStatus:
        return ModelTuiStatus(
            models=2,
            delegation=delegation,
            hooks=hooks,
        )

    return OnexTui(root=_root, runner=runner, load_status=status)


async def _open(pilot: Pilot[int]) -> None:
    for _ in range(4):
        await pilot.pause()
    await _type(pilot, "/delegation")
    await pilot.press("enter")
    for _ in range(8):
        await pilot.pause()


def _above(upper: Widget, lower: Widget) -> bool:
    """``upper`` is drawn whole above ``lower``, and inside the screen."""
    bottom = upper.region.y + upper.region.height
    return upper.region.height > 0 and bottom <= lower.region.y


@pytest.mark.parametrize("size", _SIZES)
async def test_a_missing_hook_row_keeps_its_state_word(size: tuple[int, int]) -> None:
    """DF1: delegation on, the hook missing: the row shows its word whole."""
    words = tui_design().delegation
    app = _app({"prompt_hook": "missing"}, delegation="hook")
    async with app.run_test(size=size) as pilot:
        await _open(pilot)
        prompt = _shown(app.screen.query_one("#hook-prompt_hook"))
    assert words.hook_missing in prompt, prompt


@pytest.mark.parametrize("size", _SIZES)
async def test_the_key_line_and_a_long_note_stay_above_the_buttons(
    size: tuple[int, int],
) -> None:
    """DF3: Claude Code missing (a two-line Claude line), then a refused Save."""
    missing = json.dumps(
        {
            "status": "off",
            "claude_code": "not_installed",
            "fix": None,
            "observations": [
                {
                    "step": "claude-plugin",
                    "status": "unavailable",
                    "evidence": {"claude_code": "not_installed"},
                },
                {"step": "claude-hooks", "status": "unavailable", "evidence": {}},
            ],
        }
    )
    rows = {**_selected_rows(connection=False), ("delegation", "status"): missing}
    app = _app({}, rows)
    async with app.run_test(size=size) as pilot:
        await _open(pilot)
        app.screen.set_all(True)
        await pilot.press("ctrl+s")  # no Save button (OMN-26688)
        for _ in range(8):
            await pilot.pause()
        buttons = app.screen.query_one("#dbuttons")
        keys = app.screen.query_one("#dkeys")
        note = app.screen.query_one("#dnote")
        keys_ok = keys.region.height > 0 and not keys.region.intersection(
            buttons.region
        )
        note_ok = _above(note, buttons)
        regions = (keys.region, note.region, buttons.region)
        key_text, note_text = _shown(keys), _shown(note)
    hint = tui_design().words["tab_hint_delegation"]
    assert keys_ok, regions
    assert note_ok, regions
    for word in hint.replace("·", " ").split():
        assert word in key_text, (word, key_text)
    assert " ".join(tui_design().words["claude_missing"].split()) in note_text, (
        note_text
    )
