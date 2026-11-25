# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Config tab, mockup page 4 (OMN-26620 AC12).

Failure modes this suite is written against (enumerated before the code):
  B1  the title is the old sentence, or has no step counter, or the counter
      does not move when models are found and ticked.
  B2  the provider list still draws the toolkit's bracketed radio buttons.
  B3  the key field shows the key, or never says it was accepted, or says so
      before any list was read with it.
  B4  the models line is the old sentence (no count, no "Space to tick").
  B5  a model row lacks its context, its In and Out price in words, or its
      capabilities, or still carries the Provider and Status columns.
  B6  the Selected line is missing, stale after a tick, or shows no marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input

from omnimarket.handlers.routing.byok_model_listing import (
    ModelByokListedModel,
    ModelByokModelListing,
)
from omnimarket.nodes.node_onex_setup_effect.cli_tui import OnexTui
from tests.unit.nodes.node_onex_setup_effect import tabs_redesign_support as sup
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _index, _Runner

pytestmark = pytest.mark.unit

_KEY = "sk-or-secret-value"


def _listing(provider: str, key: str) -> ModelByokModelListing:
    models = [
        ModelByokListedModel(
            id="gemini-flash",
            name="gemini-flash",
            context_tokens=1_000_000,
            input_usd_per_million=0.08,
            output_usd_per_million=0.30,
            supported_parameters=("tools",),
            input_modalities=("image",),
        ),
        ModelByokListedModel(
            id="qwen3-27b",
            name="qwen3-27b",
            context_tokens=32_000,
            input_usd_per_million=0.10,
            output_usd_per_million=0.30,
            supported_parameters=("tools",),
        ),
        *(
            ModelByokListedModel(id=f"zz-model-{i:02d}", name=f"zz-model-{i:02d}")
            for i in range(22)
        ),
    ]
    return ModelByokModelListing(
        provider=provider, outcome="listed", models=tuple(models)
    )


def _app() -> OnexTui:
    return sup.app(list_models=_listing, load_index=_index)


async def _open(pilot, app: OnexTui, *, key: str | None = _KEY) -> None:
    await sup.open_tab(pilot, app, "config")
    app.screen.query_one("#provider").value = "openrouter"
    await pilot.pause()
    if key is not None:
        app.screen.query_one("#key", Input).value = key
        await pilot.pause(1.4)  # the list loads once the key stops changing
        await sup.settle(pilot)


def _line(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"no line holds {needle!r}:\n{text}")


@pytest.mark.parametrize("size", sup.SIZES)
async def test_title_and_step_counter_follow_the_work(size: tuple[int, int]) -> None:
    """B1: step 1 until models are found, 2 once listed, 3 once one is ticked."""
    app = _app()
    async with app.run_test(size=size) as pilot:
        await _open(pilot, app, key=None)
        first = sup.screen_text(app)
        await _open(pilot, app)
        listed = sup.screen_text(app)
        await pilot.press("space")
        await sup.settle(pilot)
        ticked = sup.screen_text(app)
    for text, step in ((first, 1), (listed, 2), (ticked, 3)):
        row = _line(text, "Add models")
        assert f"step {step} of 3" in row, row
        assert row.index("Add models") < row.index(f"step {step} of 3")
    assert "Models for delegation" not in first


@pytest.mark.parametrize("size", sup.SIZES)
async def test_provider_list_has_radio_markers(size: tuple[int, int]) -> None:
    """B2: one filled marker on the chosen provider, hollow ones on the rest."""
    app = _app()
    async with app.run_test(size=size) as pilot:
        await _open(pilot, app, key=None)
        text = sup.screen_text(app)
    assert "● OpenRouter" in text
    assert "○ Google Gemini" in text
    assert "▐" not in text
    assert "▌" not in text


@pytest.mark.parametrize("size", sup.SIZES)
async def test_key_shows_dots_and_a_valid_mark_only_after_a_list_was_read(
    size: tuple[int, int],
) -> None:
    """B3: dots, never the key; the mark follows the list, not the typing."""
    app = _app()
    async with app.run_test(size=size) as pilot:
        await _open(pilot, app, key=None)
        before = sup.screen_text(app)
        await _open(pilot, app)
        after = sup.screen_text(app)
    assert "✓ valid" not in before
    assert "••••" in after
    assert "✓ valid" in after
    assert _KEY not in after
    assert "secret" not in after


@pytest.mark.parametrize("size", sup.SIZES)
async def test_models_found_line(size: tuple[int, int]) -> None:
    """B4."""
    app = _app()
    async with app.run_test(size=size) as pilot:
        await _open(pilot, app)
        text = sup.screen_text(app)
    assert "24 models found · Space to tick" in text, text


@pytest.mark.parametrize("size", sup.SIZES)
async def test_model_rows_show_context_price_in_words_and_capabilities(
    size: tuple[int, int],
) -> None:
    """B5."""
    app = _app()
    async with app.run_test(size=size) as pilot:
        await _open(pilot, app)
        text = sup.screen_text(app)
    gemini = _line(text, "gemini-flash")
    assert "[ ]" in gemini, gemini
    assert "1M" in gemini, gemini
    assert "$0.08 in · $0.30 out" in gemini, gemini
    assert "tools, vision" in gemini, gemini
    # Operator feedback (2026-11-25) added the column names and a search box to this list,
    # so "Search models" is now expected here.
    for old in ("READY", "UNTESTED", "▍"):
        assert old not in text, (old, text)
    assert "Search models" in text, text
    if size[0] > 100:
        qwen = _line(text, "qwen3-27b")
        assert "32k" in qwen, qwen
        assert "$0.10 in · $0.30 out" in qwen, qwen


@pytest.mark.parametrize("size", sup.SIZES)
async def test_selected_line_follows_the_ticks(size: tuple[int, int]) -> None:
    """B6."""
    app = _app()
    async with app.run_test(size=size) as pilot:
        await _open(pilot, app)
        before = sup.screen_text(app)
        await pilot.press("space")  # the first row, gemini-flash
        await sup.settle(pilot)
        after = sup.screen_text(app)
    assert "Selected  none yet" in before, before
    assert "Selected  ◈ gemini-flash" in after, after
    assert "[✓] gemini-flash" in after or "[✓]  gemini-flash" in after, after


def _failing_listing(tmp_path: Path):
    def listing(provider: str, key: str) -> ModelByokModelListing:
        raise OSError(13, "Permission denied", str(sup.planted_path(tmp_path)))

    return listing


@pytest.mark.parametrize("size", sup.SIZES)
async def test_no_raw_error_on_screen_when_the_list_cannot_be_read(
    size: tuple[int, int], tmp_path: Path
) -> None:
    """AC9, B7: an OSError with a path in the listing says what to do, in words."""
    app = sup.app(list_models=_failing_listing(tmp_path), load_index=_index)
    async with app.run_test(size=size) as pilot:
        await _open(pilot, app)
        text = sup.screen_text(app)
    sup.assert_no_raw(text, tmp_path)
    assert "Run /doctor" in text, text


@pytest.mark.parametrize("size", sup.SIZES)
async def test_no_raw_error_on_screen_when_saving_fails(
    size: tuple[int, int], tmp_path: Path
) -> None:
    """AC9, B8: a save whose command prints a raw cause shows plain words only."""
    run = _Runner([], code=1, errors=f"Error: {sup.raw_text(tmp_path)}")
    app = sup.app(run, list_models=_listing, load_index=_index)
    async with app.run_test(size=size) as pilot:
        await _open(pilot, app)
        await pilot.press("space")
        await pilot.press("ctrl+s")
        await sup.settle(pilot, 25)
        text = sup.screen_text(app)
    sup.assert_no_raw(text, tmp_path)
    assert "Run /doctor" in text, text
