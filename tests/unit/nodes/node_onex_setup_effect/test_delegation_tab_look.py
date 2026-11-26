# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Delegation tab looks like /config (OMN-26688).

Operator 2026-11-26, on the Delegation tab ("delegetaion?"), chose "Design
pass like Config": a Turn on that reads as a button, and the order table
framed with a line under its column names and striped rows, as /config's.

Failure modes:
  L1  the toggle is not a rounded outline three rows tall around a filled
      track, as a phone's switch (operator 2026-11-26: "exactly same" as
      the picture), or ON is not green, or the track is not its theme fill.
  L6  on some onex theme the toggle's outline is too close to the ground
      behind it to be seen (the filled grey vanished on the dark theme).
  L2  the hook's line sits on a line of its own, apart from the toggle.
  L3  the order table's column names have no line under them, or are not
      the layout's titles.
  L4  a column name does not start where its column starts.
  L5  the rows are not striped.
"""

from __future__ import annotations

import json

import pytest
from textual.color import Color
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static

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


async def test_the_toggle_is_a_rounded_outline() -> None:
    """L1."""
    app = await _delegation()
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/delegation")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, DelegationScreen)
        screen = app.screen
        button = screen.query_one("#dturn", Button)
        switch = screen.query_one("#dswitch", Horizontal)
        states = [name for name in ("-on", "-off", "-busy") if button.has_class(name)]
        assert len(states) == 1, states
        assert switch.has_class(states[0]), switch.classes
        sides = (
            switch.styles.border_top,
            switch.styles.border_right,
            switch.styles.border_bottom,
            switch.styles.border_left,
        )
        assert {kind for kind, _ in sides} == {"round"}, sides
        assert switch.outer_size.height == 3, switch.outer_size
        fill = "toggle-on" if states == ["-on"] else "toggle-off"
        assert button.styles.background == Color.parse(app.theme_variables[fill])
        if states == ["-on"]:
            assert sides[0][1] == Color.parse(app.theme_variables["success"])


async def test_the_hook_line_is_on_the_toggle_row() -> None:
    """L2."""
    app = await _delegation()
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/delegation")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, DelegationScreen)
        screen = app.screen
        row = screen.query_one("#dturn-row", Horizontal)
        hook = screen.query_one("#hook-prompt_hook", Static)
        assert hook.parent is row
        assert screen.query_one("#dswitch", Horizontal).parent is row


@pytest.mark.parametrize("delegation", ["hook", "off"])
async def test_the_toggle_stands_out_on_every_theme(delegation: str) -> None:
    """L6."""
    design = tui_design()
    names = [design.theme.name, *(theme.name for theme in design.themes.values())]
    hooks = {"prompt_hook": "installed" if delegation == "hook" else "missing"}
    app = sup.app(delegation=delegation, hooks=hooks)
    faint: list[tuple[str, float]] = []
    async with app.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, app, "delegation")
        for name in names:
            app.theme = name
            await sup.settle(pilot, 3)
            switch = app.screen.query_one("#dswitch", Horizontal)
            ground = app.screen.query_one("#deleg").styles.background
            if ground.a == 0:  # the terminal's own ground: dark or light
                ground = (
                    Color(0, 0, 0) if app.current_theme.dark else Color(255, 255, 255)
                )
            outline = ground + switch.styles.border_top[1]
            gap = abs(outline.brightness - ground.brightness)
            if gap < 0.2:
                faint.append((name, round(gap, 3)))
    assert faint == [], faint


async def test_the_order_table_has_a_line_under_its_column_names() -> None:
    """L3."""
    app = await _delegation()
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/delegation")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, DelegationScreen)
        screen = app.screen
        table = screen.query_one("#selected")
        head = table.query_one("#mt-head", Horizontal)
        rows = table.query_one("#mt-rows", DataTable)
        assert head.display
        assert rows.show_header is False
        assert head.styles.border_bottom[0] == "solid"
        labels = [str(s.render()).strip() for s in head.query(Static)]
        assert labels == ["Model", "Context", "In $/1M", "Out $/1M", "Status"]


async def test_each_column_name_starts_where_its_column_starts() -> None:
    """L4."""
    app = await _delegation()
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/delegation")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, DelegationScreen)
        screen = app.screen
        table = screen.query_one("#selected")
        head = table.query_one("#mt-head", Horizontal)
        rows = table.query_one("#mt-rows", DataTable)
        x = rows.content_region.x
        for column, label in zip(
            rows.columns.values(), head.query(Static), strict=True
        ):
            assert label.region.x == x, (str(column.label), label.region.x, x)
            x += column.get_render_width(rows)


async def test_the_rows_are_striped() -> None:
    """L5."""
    app = await _delegation()
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/delegation")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, DelegationScreen)
        screen = app.screen
        table = screen.query_one("#selected")
        rows = table.query_one("#mt-rows", DataTable)
        assert rows.zebra_stripes is True
        assert rows.row_count == 3
