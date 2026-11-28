# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every tab is spaced, fits a 120-column terminal and says each thing once.

Operator 2026-11-13: "make cli/tui neat and clean, no unnecessary stuff", "give
proper space in TUI". Read from renders of the real TUI that day at 160x46 and
120x36. Failure modes:

  L1  a tab's key line is cut off, or runs into its buttons, at 120 columns.
  L2  a tab's sections touch: no blank line between the choice and the field
      under it, or between the parts of /delegation.
  L3  the summary counts "0 servers" when there are none.
  L4  /model names Auto's full label twice (the "Now:" line and its button).
"""

from __future__ import annotations

import pytest
from textual.widget import Widget

from omnimarket.models.model_catalog_entry import ModelCatalogEntry
from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiSelection,
    _summary_text,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _Runner,
    _selected_rows,
    _type,
)

pytestmark = pytest.mark.unit

_KEYS = {
    "/config": ("#keys", "#buttons"),
    "/model": ("#pkeys", None),
    "/delegation": ("#dkeys", "#dbuttons"),
    "/dashboard": ("#bkeys", "#bbuttons"),
}


def _hint(tab: str, words: dict[str, str]) -> str:
    """The tab's footer line (OMN-26620 mockups): plain words from the design."""
    name = tab.strip("/")
    if name == "config":  # drawn from footer.screens.config (AC4)
        footer = tui_design().footer
        return footer.separator.join(
            f"{key.label} {key.words}" for key in footer.screens["config"].keys
        )
    return words[
        "tab_hint_dashboard_stopped" if name == "dashboard" else f"tab_hint_{name}"
    ]


async def _open(pilot, tab: str) -> None:
    await _type(pilot, tab)
    await pilot.press("enter")
    for _ in range(8):
        await pilot.pause()


def _gap(upper: Widget, lower: Widget) -> int:
    return lower.region.y - (upper.region.y + upper.region.height)


def _shown(widget: Widget) -> str:
    """The text a widget draws in its region, line by line."""
    lines = widget.render_lines(widget.region.reset_offset)
    text = " ".join("".join(seg.text for seg in line) for line in lines)
    return " ".join(text.split())


@pytest.mark.parametrize("tab", list(_KEYS))
async def test_the_key_line_shows_whole_above_the_buttons(tab: str) -> None:
    """L1."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(120, 36)) as pilot:
        await _open(pilot, tab)
        keys_id, buttons_id = _KEYS[tab]
        keys = app.screen.query_one(keys_id)
        text = _shown(keys)
        hint = _hint(tab, tui_design().words)
        for word in hint.replace("·", " ").split():
            assert word in text, (word, text)
        if buttons_id:  # the footer line is whole and clear of the buttons
            buttons = app.screen.query_one(buttons_id)
            assert not keys.region.intersection(buttons.region), (
                keys.region,
                buttons.region,
            )


async def test_config_key_box_sits_beside_the_provider_list_with_room() -> None:
    """L2 for /config (OMN-26620): the key box is right of the providers, a gap apart."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(120, 36)) as pilot:
        await _open(pilot, "/config")
        providers = app.screen.query_one("#provider").region
        key_box = app.screen.query_one("#keybox").region
        assert key_box.x - (providers.x + providers.width) >= 1, (providers, key_box)


@pytest.mark.parametrize(
    ("tab", "upper", "lower"),
    [("/delegation", "#on-state", "#claude")],
)
async def test_sections_have_room_between_them(
    tab: str, upper: str, lower: str
) -> None:
    """L2."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(120, 36)) as pilot:
        await _open(pilot, tab)
        assert _gap(app.screen.query_one(upper), app.screen.query_one(lower)) >= 1


async def test_no_servers_are_not_counted() -> None:
    """L3."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(120, 36)) as pilot:
        await _open(pilot, "/config")
        assert "0 servers" not in str(app.screen.query_one(".summary").render())


@pytest.mark.parametrize(
    ("urls", "count"),
    [
        (("http://one.test", "http://one.test"), "1 server"),
        (("http://one.test", "http://two.test"), "2 servers"),
    ],
)
def test_summary_preserves_distinct_positive_server_counts(
    urls: tuple[str, ...], count: str
) -> None:
    entries = tuple(
        ModelCatalogEntry(
            provider="local",
            model_id=str(i),
            display_name=str(i),
            deployment="local",
            server_url=url,
        )
        for i, url in enumerate(urls)
    )
    assert f" · {count} · " in _summary_text(ModelTuiSelection(entries=entries)).plain


async def test_model_names_auto_once() -> None:
    """L4."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(120, 36)) as pilot:
        await _open(pilot, "/model")
        label = tui_design().model_table.auto_label
        texts = [_shown(app.screen.query_one(i)) for i in ("#pnow", "#auto")]
        # never twice; with Auto already on there is no button to choose it
        assert sum(label in " ".join(t.split()) for t in texts) <= 1
        assert not app.screen.query_one("#auto").display
