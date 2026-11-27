"""OMN-26620: /help and the Model tab's Auto button against the approved
mockups v2 (real-terminal pass at 120x36, 2026-11-25).

  H1  /help prints one dense paragraph: the commands cannot be scanned, and
      the keys are buried in prose (mockup 10: bold keys, muted words).
  A1  the Model tab offers "Use Auto (A)" while Auto is already on.
  A2  with a model pinned first, the button to go back to Auto is still there.
"""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _log_text,
    _Runner,
    _selected_rows,
    _type,
)

pytestmark = pytest.mark.timeout(60)


async def test_help_lists_each_command_on_its_own_row_and_the_keys() -> None:
    """H1."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(120, 40)) as pilot:
        await _type(pilot, "/help")
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        lines = [" ".join(line.split()) for line in _log_text(app).splitlines()]
    hints = tui_design().palette.hints
    for name in ("config", "model", "delegation", "dashboard", "doctor", "metering"):
        row = [
            line for line in lines if f"/{name} " in f"{line} " and hints[name] in line
        ]
        assert len(row) == 1, (name, lines)
    assert any("ctrl+p palette" in line and "esc close" in line for line in lines), (
        lines
    )
    assert not any(len(line) > 118 for line in lines), lines


def _rows(first: bool) -> str:
    row: dict[str, object] = {
        "provider": "local",
        "set_up": True,
        "models": ["llama3.2", "qwen3:8b"],
    }
    if first:
        row |= {"tried_first": True, "first_model": "llama3.2"}
    return json.dumps(row)


@pytest.mark.parametrize(("first", "shown"), [(False, False), (True, True)])
async def test_the_auto_button_shows_only_when_a_model_is_first(
    first: bool, shown: bool
) -> None:
    """A1, A2."""
    app = _app(_Runner([], by_command={("models", "list"): _rows(first)}))
    async with app.run_test(size=(120, 36)) as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        for _ in range(8):
            await pilot.pause()
        assert app.screen.query_one("#auto").display is shown


def test_help_keys_wrap_between_pairs_never_inside_one() -> None:
    """H2: at 80x24 the real terminal broke the keys line inside "ctrl+c quit"."""
    from rich.console import Console

    from omnimarket.nodes.node_onex_setup_effect import cli_tui

    pairs = tui_design().words["help_keys"].split(" · ")
    for width in range(48, 121):  # the main screen's narrowest log is wider
        console = Console(width=width, record=True, color_system=None)
        console.print(cli_tui.help_renderable([]))
        shown = console.export_text().replace("\u00a0", " ")
        for pair in pairs:
            assert pair in shown, (width, pair, shown)
