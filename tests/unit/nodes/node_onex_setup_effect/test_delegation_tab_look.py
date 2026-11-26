# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Delegation tab looks like /config (OMN-26688).

Operator 2026-11-26: Claude Code's switch is a phone's switch ("i wnat exact
same"), on the agent list of the operator's picture ("do like this"). The
order table left the tab the same day (the Model tab lists the models), and
with it L3 to L5 (its column line, column starts and stripes).

Failure modes:
  L1  Claude Code's switch is not cli_toggle's one-line pill of its width
      (operator 2026-11-26: "i want this"), in the open theme's colours.
  L6  on some onex theme the switch's knob or word is too close to the
      ground behind it to be seen (the filled grey vanished on the dark
      theme).
  L2  the hook's line is not directly under Claude Code's row.
  L7  on some onex theme a switch's track is too close to the ground to show
      the pill's shape (the light theme's grey track vanished, 2026-11-26).
"""

from __future__ import annotations

import json

import pytest
from textual.color import Color
from textual.widgets import Static

from omnimarket.nodes.node_onex_setup_effect.cli_toggle import WIDTH, ToggleSwitch, pill
from omnimarket.nodes.node_onex_setup_effect.cli_tui import DelegationScreen
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect import tabs_redesign_support as sup
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _Runner
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_local_servers import (
    _app,
    _open,
    _rows,
)

pytestmark = pytest.mark.unit


async def _delegation():
    runner = _Runner(
        [],
        by_command={
            ("models", "list"): _rows(),
            ("models", "connections"): json.dumps({"connections": []}),
        },
    )
    return _app(runner)


async def test_claude_codes_switch_is_the_pill() -> None:
    """L1."""
    app = await _delegation()
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/delegation")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, DelegationScreen)
        screen = app.screen
        switch = screen.query_one("#dturn", ToggleSwitch)
        states = [name for name in ("-on", "-off", "-busy") if switch.has_class(name)]
        assert len(states) == 1, states
        on = {"-on": True, "-off": False, "-busy": None}[states[0]]
        assert switch.on is on
        drawn = switch.drawn.plain
        assert len(drawn) == WIDTH, drawn
        assert "\n" not in drawn, drawn
        assert (switch.outer_size.width, switch.outer_size.height) == (WIDTH, 1)
        word = "ON" if on else "OFF"
        expected = pill(on, word, track="#000", knob="#000", ink="#000")
        assert drawn == expected.plain, (drawn, expected.plain)
        paint = screen.toggle_paint(on)
        found = app.get_css_variables()
        assert paint["track"] == found["toggle-on" if on else "toggle-off"], paint


async def test_the_hook_line_is_under_claude_codes_row() -> None:
    """L2."""
    app = sup.app(hooks={"prompt_hook": "missing"})
    async with app.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        assert isinstance(app.screen, DelegationScreen)
        agents = list(app.screen.query_one("#agents").children)
        ids = [child.id for child in agents]
        hook = app.screen.query_one("#hook-prompt_hook", Static)
        assert ids.index("hook-prompt_hook") == ids.index("row-claude_code") + 1, ids
        assert hook.display
        assert "! not installed" in str(hook.render())


@pytest.mark.parametrize("delegation", ["hook", "off"])
async def test_the_toggle_stands_out_on_every_theme(delegation: str) -> None:
    """L6."""
    design = tui_design()
    names = [design.theme.name, *(theme.name for theme in design.themes.values())]
    hooks = {"prompt_hook": "installed" if delegation == "hook" else "missing"}
    app = sup.app(delegation=delegation, hooks=hooks)
    faint: list[tuple[str, str, float]] = []
    async with app.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        for name in names:
            app.theme = name
            await sup.settle(pilot, 3)
            assert isinstance(app.screen, DelegationScreen)
            paint = app.screen.toggle_paint(delegation == "hook")
            ground = app.screen.query_one("#deleg").styles.background
            if ground.a == 0:  # the terminal's own ground: dark or light
                ground = (
                    Color(0, 0, 0) if app.current_theme.dark else Color(255, 255, 255)
                )
            for part in ("knob", "ink"):
                shown = ground + Color.parse(paint[part])
                gap = abs(shown.brightness - ground.brightness)
                if gap < 0.2:
                    faint.append((name, part, round(gap, 3)))
    assert faint == [], faint


@pytest.mark.parametrize("on", [True, False])
async def test_the_track_stands_apart_on_every_theme(on: bool) -> None:
    """L7: some colour channel differs from the ground by 24 or more."""
    design = tui_design()
    names = [design.theme.name, *(theme.name for theme in design.themes.values())]
    names = [name for name in names if name.startswith("onex")]
    app = sup.app()
    lost: list[tuple[str, int]] = []
    async with app.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        for name in names:
            app.theme = name
            await sup.settle(pilot, 3)
            assert isinstance(app.screen, DelegationScreen)
            track = Color.parse(app.screen.toggle_paint(on)["track"])
            ground = app.screen.query_one("#deleg").styles.background
            if ground.a == 0:
                ground = (
                    Color(0, 0, 0) if app.current_theme.dark else Color(255, 255, 255)
                )
            gap = max(abs(a - b) for a, b in zip(track.rgb, ground.rgb, strict=True))
            if gap < 24:
                lost.append((name, gap))
    assert names, design
    assert lost == [], lost
