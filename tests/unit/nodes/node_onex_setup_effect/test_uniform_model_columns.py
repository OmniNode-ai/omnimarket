"""OMN-26620: one way to show a model's facts in every tab (operator
screenshots 2026-11-26: "why in out repeat in all rows?" and "in all tabs
reuse the code also - make it consistent and uniform").

  U1  the Config list writes "in" and "out" into every row's price, where the
      Model tab names them once, in its column titles.
  U2  a fact the tabs share (context, input price, output price) has a
      different column title, or different words in its cell, per tab.
  U3  the Delegation list drops the column names, the context or the prices
      the Config and Model lists show for the same models.
  U4  in an 80-column window the Delegation list cuts its last column
      ("not tested ye") because the position takes a column of its own,
      where the Model tab puts its marker in the name cell.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable

from omnimarket.nodes.node_onex_setup_effect.cli_model_table import ModelTable
from tests.unit.nodes.node_onex_setup_effect.tabs_redesign_support import entry

pytestmark = pytest.mark.timeout(60)

_ENTRIES = [
    entry("aion-labs/aion-2.0", context=131_000, price_in="0.8", price_out="1.6"),
    entry("amazon/nova-micro-v1", context=128_000, price_in="0.035", price_out="0.14"),
]


class _Host(App[None]):
    def compose(self) -> ComposeResult:
        yield ModelTable(selectable=True, layout="found", id="found")
        yield ModelTable(selectable=False, layout="pick", id="pick")
        yield ModelTable(selectable=False, layout="order", id="order")


async def _tables() -> dict[str, tuple[list[str], list[list[str]]]]:
    app = _Host()
    async with app.run_test(size=(140, 40)) as pilot:
        shown: dict[str, tuple[list[str], list[list[str]]]] = {}
        for name in ("found", "pick", "order"):
            table = app.query_one(f"#{name}", ModelTable)
            table.set_entries(_ENTRIES)
            for _ in range(3):
                await pilot.pause()
            rows = table.query_one("#mt-rows", DataTable)
            assert rows.show_header or table.query_one("#mt-head").display, name
            titles = [str(column.label).strip() for column in rows.columns.values()]
            cells = [
                [str(cell).strip() for cell in rows.get_row_at(index)]
                for index in range(rows.row_count)
            ]
            shown[name] = (titles, cells)
        return shown


async def test_config_prices_name_in_and_out_once_in_the_titles() -> None:
    """U1."""
    titles, cells = (await _tables())["found"]
    assert "In $/1M" in titles, titles
    assert "Out $/1M" in titles, titles
    for row in cells:
        assert not any(" in" in cell or " out" in cell for cell in row), row


async def test_shared_facts_read_the_same_in_both_tabs() -> None:
    """U2."""
    shown = await _tables()
    for fact in ("Context", "In $/1M", "Out $/1M"):
        columns = {}
        for name, (titles, cells) in shown.items():
            assert fact in titles, (name, fact, titles)
            index = titles.index(fact)
            columns[name] = [row[index] for row in cells]
        assert columns["found"] == columns["pick"] == columns["order"], (fact, columns)


async def test_every_list_names_its_columns_the_same_way() -> None:
    """U3."""
    shown = await _tables()
    for name, (titles, _) in shown.items():
        assert "Model" in titles, (name, titles)


class _Order(App[None]):
    def compose(self) -> ComposeResult:
        yield ModelTable(selectable=False, layout="order", id="order")


async def test_the_delegation_list_fits_an_80_column_window() -> None:
    """U4."""
    app = _Order()
    async with app.run_test(size=(76, 20)) as pilot:
        table = app.query_one("#order", ModelTable)
        table.set_entries(
            [
                entry(
                    "qwen/qwen3.8-27b-instruct-long-name",
                    context=131_000,
                    price_in="0.1",
                    price_out="0.3",
                    status="untested",
                ),
            ]
        )
        for _ in range(4):
            await pilot.pause()
        rows = table.query_one("#mt-rows", DataTable)
        cells = [str(cell).strip() for cell in rows.get_row_at(0)]
        assert cells[0].startswith("1 "), cells
        shown = "\n".join(
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()
        )
        assert "not tested yet" in shown, shown
