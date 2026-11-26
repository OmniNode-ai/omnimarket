# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Delegation tab looks like /config (OMN-26688).

Operator 2026-11-26, on the Delegation tab ("delegetaion?"), chose "Design
pass like Config": a Turn on that reads as a button, and the order table
framed with a line under its column names and striped rows, as /config's.

Failure modes:
  L1  the toggle is drawn as plain text, or with more than one state's fill:
      green on, grey off, dimmer grey while reading or switching.
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


async def test_the_toggle_is_filled_by_its_state() -> None:
    """L1."""
    app = await _delegation()
    async with app.run_test(size=(170, 46)) as pilot:
        await _open(pilot, "/delegation")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, DelegationScreen)
        screen = app.screen
        button = screen.query_one("#dturn", Button)
        fills = {
            "-on": "success",
            "-off": "panel-lighten-2",
            "-busy": "panel-lighten-1",
        }
        shown = [name for name in fills if button.has_class(name)]
        assert len(shown) == 1, shown
        fill = Color.parse(app.theme_variables[fills[shown[0]]])
        assert button.styles.background == fill


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
        assert screen.query_one("#dturn", Button).parent is row


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
