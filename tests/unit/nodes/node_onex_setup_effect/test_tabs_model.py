# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model tab, mockup page 5 (OMN-26620 AC12).

Failure modes this suite is written against (enumerated before the code):
  C1  the line above the table is the old "Choose ... / Now:" pair, or it does
      not name the model tried first (or Auto when none is chosen).
  C2  the table keeps the old seven-column layout: Provider, "Ctx", a joined
      "Price $/1M (in / out)", capital status pills, a search box, a bar.
  C3  rows are sorted away from the order delegation tries them in, the tried
      first row has no filled marker, or a status is not a plain word.
  C4  a model that needs a key reads as READY or UNAVAILABLE in capitals.
  C5  d does not open the details of the row under the cursor.
  C6  a refused change shows the raw cause (errno, path, exception, env name).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiSelection
from tests.unit.nodes.node_onex_setup_effect import tabs_redesign_support as sup
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _Runner

pytestmark = pytest.mark.unit


def _line(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"no line holds {needle!r}:\n{text}")


async def _text(size: tuple[int, int]) -> str:
    app = sup.app()
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, "model")
        return sup.screen_text(app)


@pytest.mark.parametrize("size", sup.SIZES)
async def test_tried_first_line_names_the_model(size: tuple[int, int]) -> None:
    """C1."""
    text = await _text(size)
    assert "Tried first: ◈ qwen3-27b" in text, text
    assert "Choose the model" not in text
    assert "Now:" not in text


@pytest.mark.parametrize("size", sup.SIZES)
async def test_tried_first_line_says_auto_when_no_model_is_chosen(
    size: tuple[int, int],
) -> None:
    """C1."""
    app = sup.app()
    async with app.run_test(size=size) as pilot:
        await sup.settle(pilot, 4)
        chosen = sup.selection()
        none = ModelTuiSelection(entries=chosen.entries)
        await app.push_screen(app._build_tab("model", none))  # type: ignore[arg-type]
        await sup.settle(pilot)
        text = sup.screen_text(app)
    assert "Tried first: ◈ Auto picks per task" in text, text


@pytest.mark.parametrize("size", sup.SIZES)
async def test_table_has_five_columns_in_words(size: tuple[int, int]) -> None:
    """C2."""
    text = await _text(size)
    header = _line(text, "Context")
    cells = header.split("│", 1)[1].replace("│", " ").split()
    assert cells[:5] == ["Model", "Context", "In", "$/1M", "Out"], header
    assert "$/1M" in header
    assert "Status" in header
    for old in ("Ctx", "Provider", "Price $/1M", "Search models", "sorted by"):
        assert old not in text, (old, text)


@pytest.mark.parametrize("size", sup.SIZES)
async def test_rows_follow_the_delegation_order_with_the_first_marked(
    size: tuple[int, int],
) -> None:
    """C3, C4."""
    text = await _text(size)
    rows = [
        _line(text, "◉"),
        _line(text, "gemini-flash"),
        _line(text, "glm-5"),
    ]
    positions = [text.index(r) for r in rows]
    assert positions == sorted(positions), rows  # the order they are tried in
    assert "◉ qwen3-27b" in rows[0], rows[0]
    assert "○ gemini-flash" in rows[1], rows[1]
    assert "○ glm-5" in rows[2], rows[2]
    for row, context, price_in, price_out, status in (
        (rows[0], "32k", "$0.10", "$0.30", "✓ ready"),
        (rows[1], "1M", "$0.08", "$0.30", "✓ ready"),
        (rows[2], "128k", "$0.20", "$0.60", "! needs a key"),
    ):
        cells = row.split("│", 1)[1].replace("│", " ").split()
        assert context in cells, row
        assert price_in in cells, row
        assert price_out in cells, row
        assert status in " ".join(cells), row
    for old in ("READY", "UNAVAILABLE", "▍"):
        assert old not in text, (old, text)


async def test_d_opens_the_details_of_the_row_under_the_cursor() -> None:
    """C5."""
    app = sup.app()
    async with app.run_test(size=(140, 40)) as pilot:
        await sup.open_tab(pilot, app, "model")
        await pilot.press("d")
        await sup.settle(pilot)
        text = sup.screen_text(app)
    assert "Model details" in text, text
    assert "qwen3-27b" in text


@pytest.mark.parametrize("size", sup.SIZES)
async def test_no_raw_error_on_screen_when_the_change_is_refused(
    size: tuple[int, int], tmp_path: Path
) -> None:
    """AC9, C6: a refused change prints a raw cause on stderr; the screen does not."""
    run = _Runner([], code=1, errors=f"Error: {sup.raw_text(tmp_path)}")
    app = sup.app(run)
    async with app.run_test(size=size) as pilot:
        await sup.open_tab(pilot, app, "model")
        await pilot.press("down")
        await pilot.press("enter")
        await sup.settle(pilot, 25)
        text = sup.screen_text(app)
    sup.assert_no_raw(text, tmp_path)
    assert "Run /doctor" in text, text
