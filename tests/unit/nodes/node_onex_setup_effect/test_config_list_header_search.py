"""OMN-26620: the Config tab's found-models list has column names and a search box.

Operator feedback on a real run: 440 models were listed with no column names and no way to
find one. The list now shows its columns (Model, Context, Price, Capabilities) and a search
box above it; typing narrows the list as you type. Red on the previous head.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Input

from omnimarket.nodes.node_onex_setup_effect.cli_model_table import ModelTable
from tests.unit.nodes.node_onex_setup_effect.tabs_redesign_support import entry


class _Host(App[None]):
    def compose(self) -> ComposeResult:
        yield ModelTable(selectable=True, layout="found", id="found")


async def _loaded(pilot: object, app: _Host) -> ModelTable:
    table = app.query_one("#found", ModelTable)
    table.set_entries(
        [
            entry(
                "aion-labs/aion-2.0", context=131_000, price_in="0.8", price_out="1.6"
            ),
            entry(
                "amazon/nova-lite-v1",
                context=300_000,
                price_in="0.06",
                price_out="0.24",
            ),
            entry(
                "anthropic/claude-haiku-5.5",
                context=1_000_000,
                price_in="0.1",
                price_out="0.5",
            ),
        ]
    )
    for _ in range(3):
        await pilot.pause()  # type: ignore[attr-defined]
    return table


async def test_the_list_shows_its_column_names() -> None:
    app = _Host()
    async with app.run_test(size=(140, 30)) as pilot:
        table = await _loaded(pilot, app)
        rows = table.query_one("#mt-rows", DataTable)
        assert rows.show_header is True
        titles = [str(column.label) for column in rows.columns.values()]
        # Operator 2026-11-26: the prices are titled as on the Model tab.
        for name in ("Model", "Context", "In $/1M", "Out $/1M", "Capabilities"):
            assert any(name in title for title in titles), titles


async def test_the_list_has_a_search_box() -> None:
    app = _Host()
    async with app.run_test(size=(140, 30)) as pilot:
        table = await _loaded(pilot, app)
        search = table.query_one("#mt-search", Input)
        assert search.display is True
        assert search.placeholder


async def test_typing_in_the_search_box_narrows_the_list() -> None:
    app = _Host()
    async with app.run_test(size=(140, 30)) as pilot:
        table = await _loaded(pilot, app)
        rows = table.query_one("#mt-rows", DataTable)
        assert rows.row_count == 3
        table.query_one("#mt-search", Input).focus()
        await pilot.press(*"nova")
        for _ in range(3):
            await pilot.pause()
        assert rows.row_count == 1
        table.query_one("#mt-search", Input).value = ""
        for _ in range(3):
            await pilot.pause()
        assert rows.row_count == 3
