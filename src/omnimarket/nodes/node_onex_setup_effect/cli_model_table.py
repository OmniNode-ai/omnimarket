# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one model table /config, /model and /delegation show (OMN-23413).

Rows are ModelCatalogEntry values from omnimarket.handlers.model_catalog;
columns, words, colours, filters and sorts come from tui_design.yaml. The
screens pass entries and read selections; none of them formats a model row.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import DataTable, Input, Label, Static

from omnimarket.models.model_catalog_entry import ModelCatalogEntry
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
    ModelTuiModelTable,
    tui_design,
)


def _table() -> ModelTuiModelTable:
    return tui_design().model_table


def _colour(token: str) -> str:
    return str(getattr(tui_design().theme, token))


def _money(value: Decimal) -> str:
    """Dollars with two decimals, more only when the price needs them."""
    text = f"{value:.2f}"
    if value != 0 and Decimal(text) != value:
        text = f"{value.normalize():f}"
    return "$" + text


def price_text(entry: ModelCatalogEntry) -> str:
    """'$0.50 / $2.00', the local word, or the unknown word; unknown is never free."""
    words = _table()
    if entry.deployment == "local":
        return words.price_local
    price_in, price_out = entry.input_usd_per_million, entry.output_usd_per_million
    if price_in is None and price_out is None:
        return words.price_variable if entry.tiered_price else words.price_unknown
    left = _money(price_in) if price_in is not None else words.unknown_mark
    right = _money(price_out) if price_out is not None else words.unknown_mark
    mark = words.price_tier_mark if entry.tiered_price else ""
    return f"{left} / {right}{mark}"


def context_text(tokens: int | None) -> str:
    if tokens is None:
        return _table().unknown_mark
    if tokens >= 1_000_000:
        whole, rest = divmod(tokens, 1_000_000)
        return f"{whole}M" if rest == 0 else f"{tokens / 1_000_000:.1f}M"
    return f"{round(tokens / 1000)}K"


def capability_text(entry: ModelCatalogEntry) -> Text:
    """Supported capabilities as coloured badges; the unknown mark when none is."""
    colours = _table().capability_colours
    supported = [label for label, state in entry.capabilities.items() if state == "yes"]
    if not supported:
        return Text(_table().unknown_mark, style=_colour("muted"))
    text = Text()
    for position, label in enumerate(supported):
        if position:
            text.append(" ")
        text.append(
            label,
            style=f"bold {_colour('background')} on {_colour(colours[position % len(colours)])}",
        )
    return text


def status_text(entry: ModelCatalogEntry) -> Text:
    word = _table().status[entry.status]
    return Text(word.text, style=f"bold {_colour(word.colour)}")


def deployment_text(entry: ModelCatalogEntry) -> Text:
    word = _table().deployment[entry.deployment]
    return Text(word.text, style=_colour(word.colour))


def entry_key(entry: ModelCatalogEntry) -> str:
    """One row per model, and per server for a model two servers run (OMN-23413)."""
    key = f"{entry.provider}\t{entry.model_id}"
    return f"{key}\t{entry.server_url}" if entry.server_url else key


def _search_blob(entry: ModelCatalogEntry, provider_label: str) -> str:
    capabilities = " ".join(k for k, v in entry.capabilities.items() if v == "yes")
    return " ".join(
        (
            provider_label,
            entry.provider,
            entry.model_id,
            entry.display_name,
            capabilities,
        )
    ).lower()


def _sort_key(entry: ModelCatalogEntry, sort: str) -> tuple[object, ...]:
    if sort == "context":
        return (-(entry.context_tokens or 0), entry.model_id)
    if sort in ("input", "output"):
        price = (
            entry.input_usd_per_million
            if sort == "input"
            else entry.output_usd_per_million
        )
        return (price is None, price or Decimal(0), entry.model_id)
    return (entry.display_name.lower(), entry.model_id)


class _Rows(DataTable[object]):
    """A row is chosen by one click, not by clicking it a second time."""

    async def _on_click(self, event: events.Click) -> None:
        # Textual also runs DataTable's own click handler after this one, and it
        # selects only a click on the cell the cursor is already on. Moving the
        # cursor to the clicked cell first makes every click such a click.
        row = event.style.meta.get("row", -1)
        column = event.style.meta.get("column", -1)
        if isinstance(row, int) and isinstance(column, int) and row >= 0 <= column:
            self.cursor_coordinate = Coordinate(row, column)


class ModelTable(Widget):
    """Search, filter and sort a model list; optionally tick any number of rows."""

    DEFAULT_CSS = """
    ModelTable { height: 1fr; }
    ModelTable #mt-search { margin-bottom: 0; }
    ModelTable #mt-bar { height: 1; color: $text-muted; }
    ModelTable DataTable { height: 1fr; }
    """
    BINDINGS = [
        Binding("space", "tick", "Tick", show=False),
        Binding("i", "details", "Details", show=False),
        Binding("f", "filter", "Filter", show=False),
        Binding("s", "sort", "Sort", show=False),
        Binding("slash", "search", "Search", show=False),
    ]

    class Ticked(Message):
        def __init__(self, table: ModelTable) -> None:
            super().__init__()
            self.table = table

    class Chosen(Message):
        def __init__(self, entry: ModelCatalogEntry) -> None:
            super().__init__()
            self.entry = entry

    def __init__(
        self,
        *,
        selectable: bool,
        provider_labels: dict[str, str] | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._selectable = selectable
        self._labels = provider_labels or {}
        self._entries: tuple[ModelCatalogEntry, ...] = ()
        self._ticked: list[str] = []
        self._filter = _table().filters[0]
        self._sort = _table().sorts[0]

    def compose(self) -> ComposeResult:
        yield Input(placeholder=_table().search_placeholder, id="mt-search")
        yield Static("", id="mt-bar")
        yield _Rows(id="mt-rows", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        rows = self.query_one("#mt-rows", DataTable)
        for column in _table().columns:
            if column.key == "pick" and not self._selectable:
                continue
            rows.add_column(column.title, key=column.key)
        self._redraw()

    def _label(self, entry: ModelCatalogEntry) -> str:
        """The row's own label (a local server's kind) before the provider's."""
        return entry.provider_label or self._labels.get(entry.provider, entry.provider)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Space ticks only where rows can be ticked; elsewhere the screen has it."""
        return not (action == "tick" and not self._selectable)

    # -- data -------------------------------------------------------------
    def set_entries(self, entries: Sequence[ModelCatalogEntry]) -> None:
        """New rows; ticks survive only for rows still present."""
        self._entries = tuple(entries)
        present = {entry_key(e) for e in self._entries}
        self._ticked = [key for key in self._ticked if key in present]
        if self.is_mounted:
            self._redraw()

    def set_ticked(self, keys: Sequence[str]) -> None:
        self._ticked = list(dict.fromkeys(keys))
        if self.is_mounted:
            self._redraw()

    @property
    def ticked(self) -> tuple[ModelCatalogEntry, ...]:
        """Ticked rows, in the order they were ticked."""
        by_key = {entry_key(e): e for e in self._entries}
        return tuple(by_key[key] for key in self._ticked if key in by_key)

    @property
    def entries(self) -> tuple[ModelCatalogEntry, ...]:
        return self._entries

    def focus_rows(self) -> None:
        """Put the keyboard on the list, so Space ticks straight away."""
        self.query_one("#mt-rows", DataTable).focus()

    def current(self) -> ModelCatalogEntry | None:
        rows = self.query_one("#mt-rows", DataTable)
        if not rows.row_count:
            return None
        key = rows.coordinate_to_cell_key(rows.cursor_coordinate).row_key.value
        return next((e for e in self._entries if entry_key(e) == key), None)

    # -- rendering --------------------------------------------------------
    def _visible(self) -> list[ModelCatalogEntry]:
        words = self.query_one("#mt-search", Input).value.lower().split()
        shown = [
            e
            for e in self._entries
            if (self._filter == _table().filters[0] or e.deployment == self._filter)
            and all(w in _search_blob(e, self._label(e)) for w in words)
        ]
        return sorted(shown, key=lambda e: _sort_key(e, self._sort))

    def _cells(self, entry: ModelCatalogEntry) -> list[object]:
        words = _table()
        cells: list[object] = []
        for column in words.columns:
            if column.key == "pick":
                if self._selectable:
                    on = entry_key(entry) in self._ticked
                    mark = words.ticked if on else words.unticked
                    cells.append(
                        Text(mark, style=_colour("success" if on else "muted"))
                    )
            elif column.key == "provider":
                label = self._label(entry)
                cells.append(Text(label, style=f"bold {_colour('primary')}"))
            elif column.key == "model":
                cells.append(Text(entry.model_id, style=_colour("foreground")))
            elif column.key == "context":
                cells.append(Text(context_text(entry.context_tokens), justify="right"))
            elif column.key == "price":
                price = price_text(entry)
                known = (
                    entry.input_usd_per_million is not None
                    or entry.deployment == "local"
                )
                cells.append(Text(price, style=_colour("accent" if known else "muted")))
            elif column.key == "capabilities":
                cells.append(capability_text(entry))
            elif column.key == "status":
                cells.append(status_text(entry))
        return cells

    def _redraw(self) -> None:
        rows = self.query_one("#mt-rows", DataTable)
        keep = self.current()
        rows.clear()
        visible = self._visible()
        for entry in visible:
            rows.add_row(*self._cells(entry), key=entry_key(entry))
        words = _table()
        ticked = f"  ·  {len(self._ticked)} ticked" if self._selectable else ""
        self.query_one("#mt-bar", Static).update(
            Text(
                f"{len(visible)} of {len(self._entries)} models  ·  "
                f"{words.filter_labels.get(self._filter, self._filter)}  ·  "
                f"sorted by {words.sort_labels.get(self._sort, self._sort)}{ticked}",
                style=_colour("muted"),
            )
        )
        if keep is not None and keep in visible:
            rows.move_cursor(row=visible.index(keep))

    @on(Input.Changed, "#mt-search")
    def _searched(self) -> None:
        self._redraw()

    # -- actions ----------------------------------------------------------
    def action_search(self) -> None:
        self.query_one("#mt-search", Input).focus()

    def action_filter(self) -> None:
        filters = _table().filters
        self._filter = filters[(filters.index(self._filter) + 1) % len(filters)]
        self._redraw()

    def action_sort(self) -> None:
        sorts = _table().sorts
        self._sort = sorts[(sorts.index(self._sort) + 1) % len(sorts)]
        self._redraw()

    def action_tick(self) -> None:
        entry = self.current()
        if not self._selectable or entry is None:
            return
        key = entry_key(entry)
        if key in self._ticked:
            self._ticked.remove(key)
        else:
            self._ticked.append(key)
        self._redraw()
        self.post_message(self.Ticked(self))

    def action_details(self) -> None:
        entry = self.current()
        if entry is not None:
            self.app.push_screen(ModelDetailsScreen(entry, self._label(entry)))

    @on(DataTable.RowSelected, "#mt-rows")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        entry = self.current()
        if entry is None:
            return
        if self._selectable:
            self.action_tick()
        else:
            self.post_message(self.Chosen(entry))


def _detail_rows(
    entry: ModelCatalogEntry, provider_label: str
) -> list[tuple[str, str]]:
    """Each row's name and words come from tui_design.yaml (model_table.details)."""
    source = entry.sources
    unknown = _table().unknown_mark
    say = _table().details

    def priced(value: Decimal | None) -> str:
        return _money(value) if value is not None else unknown

    def sourced(text: str, field: str) -> str:
        return f"{text}  ({source[field]})" if field in source else text

    rows = [
        (say["provider"], provider_label),
        *([(say["server"], entry.server_url)] if entry.server_url else []),
        (say["model"], entry.model_id),
        (say["name"], entry.display_name),
        (say["runs"], _table().deployment[entry.deployment].text),
        (say["context"], sourced(context_text(entry.context_tokens), "context")),
        (say["max_output"], context_text(entry.max_output_tokens)),
        (
            say["input"],
            sourced(
                f"{priced(entry.input_usd_per_million)} {say['per_million']}", "price"
            ),
        ),
        (say["output"], f"{priced(entry.output_usd_per_million)} {say['per_million']}"),
        (say["cache_read"], priced(entry.cache_read_usd_per_million)),
        (say["cache_write"], priced(entry.cache_write_usd_per_million)),
    ]
    if entry.tiered_price:
        rows.append((say["pricing"], say["tiered"]))
    for label, state in entry.capabilities.items():
        rows.append((label, say.get(state, unknown)))
    rows.append((say["status"], _table().status[entry.status].text))
    if entry.status_reason:
        rows.append((say["why"], entry.status_reason))
    if entry.metadata_fetched_at:
        stale = f"  {say['stale']}" if entry.metadata_stale else ""
        rows.append(
            (
                say["metadata"],
                say["fetched"].format(at=entry.metadata_fetched_at) + stale,
            )
        )
    return rows


class ModelDetailsScreen(ModalScreen[None]):
    """Everything known about one model, with where each value came from."""

    CSS = """
    ModelDetailsScreen { align: center middle; }
    #md { width: 80; height: auto; max-height: 90%; border: round $accent; padding: 1 2; background: $panel; }
    #md Horizontal { height: auto; }
    #md .mdk { width: 14; color: $text-muted; }
    """
    BINDINGS = [Binding("escape,i,q", "close", "Close")]

    def __init__(self, entry: ModelCatalogEntry, provider_label: str) -> None:
        super().__init__()
        self._entry = entry
        self._label = provider_label

    def compose(self) -> ComposeResult:
        with Vertical(id="md"):
            yield Label(
                Text(_table().details["title"], style=f"bold {_colour('primary')}")
            )
            for key, value in _detail_rows(self._entry, self._label):
                with Horizontal():
                    yield Static(key, classes="mdk")
                    yield Static(value)

    def action_close(self) -> None:
        self.dismiss(None)
