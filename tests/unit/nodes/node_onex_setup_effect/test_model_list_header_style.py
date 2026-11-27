"""OMN-26620: the model list's column header and the space around its search
box (operator screenshot 2026-11-26, Config tab, 441 models, a terminal
without true colour).

  M1  in ANSI colour the header is a bright blue bar with white text
      (Textual's default), louder than anything else on the screen.
  M2  in true colour the header text is the full foreground white too.
  M3  the search box sits on the header row's edge with no space between.
"""

from __future__ import annotations

import pytest
from rich.color import ColorType
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Input

from omnimarket.nodes.node_onex_setup_effect.cli_model_table import ModelTable
from tests.unit.nodes.node_onex_setup_effect.tabs_redesign_support import entry

pytestmark = pytest.mark.timeout(60)


class _Host(App[None]):
    def __init__(self, *, ansi: bool) -> None:
        super().__init__(ansi_color=ansi)

    def compose(self) -> ComposeResult:
        yield ModelTable(selectable=True, layout="found", id="found")


async def _header(ansi: bool):
    app = _Host(ansi=ansi)
    async with app.run_test(size=(120, 30)) as pilot:
        table = app.query_one("#found", ModelTable)
        table.set_entries(
            [
                entry(
                    "aion-labs/aion-2.0",
                    context=131_000,
                    price_in="0.8",
                    price_out="1.6",
                ),
                entry(
                    "amazon/nova-lite-v1",
                    context=300_000,
                    price_in="0.06",
                    price_out="0.24",
                ),
            ]
        )
        for _ in range(4):
            await pilot.pause()
        rows = table.query_one("#mt-rows", DataTable)
        search = table.query_one("#mt-search", Input)
        style = rows.get_component_rich_style("datatable--header")
        return style, search.region, rows.region


async def test_ansi_header_is_quiet() -> None:
    """M1."""
    style, _, _ = await _header(ansi=True)
    assert style.bgcolor is None or style.bgcolor.is_default, style
    assert style.color is not None, style
    assert not style.color.is_default, style
    assert style.bold, style


async def test_true_colour_header_text_is_muted() -> None:
    """M2."""
    style, _, _ = await _header(ansi=False)
    assert style.color is not None
    assert style.color.type is not ColorType.DEFAULT
    red, green, blue = style.color.get_truecolor()
    luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
    assert luminance < 0.75, (style, luminance)


async def test_a_row_of_space_between_search_and_header() -> None:
    """M3."""
    _, search, rows = await _header(ansi=True)
    assert rows.y - search.bottom >= 1, (search, rows)
