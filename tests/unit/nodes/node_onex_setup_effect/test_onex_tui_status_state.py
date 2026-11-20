# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The TUI shows Claude Code delegation as it is (OMN-23413).

Read 2026-11-13 on the real CLI: hook delegation was on (the switch bit set,
`onex delegation status` "on"), yet a fresh TUI drew the /delegation switch off
and the status bar said "gateway off", because both read only the gateway.
A Save from that screen would have switched delegation off. The status bar also
showed the folder onex started in, which nothing depends on since delegation
became system-wide, and counted "1 models". Failure modes:

  S1  a fresh /delegation draws the combined switch other than as it is.
  S2  the status bar does not say whether delegation is on.
  S3  the status bar shows a folder.
  S4  a count of one reads "1 models".
  S5  the hook read maps to the wrong state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import RadioButton, Static

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiSelection,
    ModelTuiStatus,
    OnexTui,
    _delegation_state,
    _summary_text,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _root,
    _Runner,
    _selected_rows,
    _type,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_basket_line import _entry

pytestmark = pytest.mark.unit


def _app(delegation: str, models: int = 2) -> OnexTui:
    async def status() -> ModelTuiStatus:
        return ModelTuiStatus(models=models, delegation=delegation)

    runner = _Runner([], by_command=_selected_rows(connection=True))
    return OnexTui(root=_root, runner=runner, load_status=status)


def _status_bar(app: OnexTui) -> str:
    return " ".join(str(app.query_one("#status", Static).render()).split())


@pytest.mark.parametrize(
    ("delegation", "switch"),
    [("off", False), ("hook", True)],
)
async def test_a_fresh_delegation_tab_shows_the_state_as_it_is(
    delegation: str, switch: bool
) -> None:
    """S1."""
    app = _app(delegation)
    async with app.run_test(size=(160, 46)) as pilot:
        for _ in range(4):
            await pilot.pause()
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        assert app.screen.any_on() is switch
        assert not app.screen.query(RadioButton)


@pytest.mark.parametrize(
    ("delegation", "word"),
    [
        ("off", "delegation_off"),
        ("hook", "delegation_hook"),
    ],
)
async def test_the_status_bar_says_the_delegation_state(
    delegation: str, word: str
) -> None:
    """S2, S3."""
    app = _app(delegation)
    async with app.run_test(size=(160, 46)) as pilot:
        for _ in range(4):
            await pilot.pause()
        bar = _status_bar(app)
        assert tui_design().words[word] in bar
        assert "gateway off" not in bar
        assert str(Path.cwd().name) not in bar.split()
        assert "~/" not in bar


async def test_one_model_reads_singular() -> None:
    """S4."""
    app = _app("off", models=1)
    async with app.run_test(size=(160, 46)) as pilot:
        for _ in range(4):
            await pilot.pause()
        assert "1 model " in _status_bar(app) + " "
        assert "1 models" not in _status_bar(app)
    summary = str(
        _summary_text(ModelTuiSelection(entries=(_entry("reflection", "Beam"),)))
    )
    assert "1 model " in summary
    assert "1 models" not in summary


@pytest.mark.parametrize(
    ("hook", "expected"),
    [
        ("on", "hook"),
        ("off", "off"),
        # The hook read failed: never claimed on, nor off (review 2026-11-15).
        (None, "unknown"),
        ("action_required", "unknown"),
    ],
)
def test_the_hook_read_maps_to_one_state(hook: str | None, expected: str) -> None:
    """S5."""
    assert _delegation_state(hook) == expected
