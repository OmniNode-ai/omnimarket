"""OMN-26620: keys typed right after Enter (real-terminal replay, 2026-11-25:
separate keystrokes with no pause between them).

  K1  /model, Enter, Tab, Tab: the Tabs reach the main screen before the
      Model tab opens, so the user ends on Model instead of Dashboard.
  K2  /dashboard, Enter, Esc, /delegation, Enter: the Esc reaches the main
      screen, Dashboard opens afterwards and swallows the next command.
"""

from __future__ import annotations

import pytest
from textual import events

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    DashboardScreen,
    DelegationScreen,
    ModelTuiStatus,
    OnexTui,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _root,
    _Runner,
    _selected_rows,
)

pytestmark = pytest.mark.timeout(60)

_NAMED = {"\r": "enter", "\t": "tab", "\x1b": "escape", "/": "slash"}


async def _status() -> ModelTuiStatus:
    return ModelTuiStatus(models=2, delegation="off")


def _burst(app: OnexTui, keys: str) -> None:
    """Every key queued at once, as a terminal delivers fast typing: nothing
    is handled between them."""
    for char in keys:
        app.post_message(events.Key(_NAMED.get(char, char), char))


async def _run(keys: str) -> object:
    app = OnexTui(
        root=_root,
        runner=_Runner([], by_command=_selected_rows(connection=False)),
        load_status=_status,
    )
    async with app.run_test(size=(120, 36)) as pilot:
        for _ in range(6):
            await pilot.pause()
        _burst(app, keys)
        for _ in range(12):
            await pilot.pause()
        return app.screen


async def test_tabs_typed_right_after_enter_move_between_tabs() -> None:
    """K1."""
    assert isinstance(await _run("/model\r\t\t"), DashboardScreen)


async def test_esc_and_a_command_typed_right_after_enter() -> None:
    """K2."""
    assert isinstance(await _run("/dashboard\r\x1b/delegation\r"), DelegationScreen)
