# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC7 and the /config model list (OMN-26620 review, 2026-11-27): a read that
lands late never undoes a newer one.

Failure modes:
  LR1  the start-up status read is slow, a read after an action lands first,
       then the slow one overwrites the status bar and the kept status.
  LR2  a late model list identical to the one shown clears the ticks made
       meanwhile (the CI flake in test_saving_config_keeps_config_open...).
  LR3  Enter in the URL field searches at once, and the search the typing had
       scheduled still runs a moment later.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from textual.pilot import Pilot
from textual.widgets import Input, Static

from omnimarket.nodes.node_onex_setup_effect.cli_model_table import entry_key
from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    SetupScreen,
    StatusStore,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _Runner,
    _wait_for_models,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_local_servers import (
    _app,
    _open,
    _rows,
)

pytestmark = pytest.mark.unit

_FOUND = json.dumps({"status": "ready", "models": ["llama3.2"]})


async def test_a_slow_first_status_read_never_overwrites_a_newer_one() -> None:
    """LR1."""
    release = asyncio.Event()
    reads = 0

    async def load() -> ModelTuiStatus:
        nonlocal reads
        reads += 1
        if reads == 1:
            await release.wait()
            return ModelTuiStatus(model="Old-Model", models=1, delegation="off")
        return ModelTuiStatus(model="New-Model", models=2, delegation="hook")

    app = OnexTui(load_status=load)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await app._refresh_status()  # the read after an action
        release.set()
        for _ in range(6):
            await pilot.pause()
        assert reads == 2
        assert app._status.model == "New-Model"
        assert "New-Model" in str(app.query_one("#status", Static).render())
    assert StatusStore.load().model == "New-Model"


def _discovers(runner: _Runner) -> int:
    return sum(1 for call in runner.calls if call[:2] == ("models", "discover"))


async def _config_on_local(pilot: Pilot[int], app: OnexTui) -> SetupScreen:
    await _open(pilot, "/config")
    screen = app.screen
    assert isinstance(screen, SetupScreen)
    screen.query_one("#provider").value = "local"
    await pilot.pause()
    return screen


async def test_a_late_identical_list_keeps_the_ticks() -> None:
    """LR2."""
    runner = _Runner(
        [],
        by_command={("models", "list"): _rows(), ("models", "discover"): _FOUND},
    )
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        screen = await _config_on_local(pilot, app)
        await _wait_for_models(pilot)
        table = screen.query_one("#found")
        keys = [entry_key(e) for e in table.entries]
        assert keys
        table.set_ticked(keys)
        await screen._fetch(
            screen.query_one("#note", Static), screen._provider(), screen._source()
        )
        assert [entry_key(e) for e in table.ticked] == keys


async def test_enter_in_the_url_field_replaces_the_pending_search() -> None:
    """LR3."""
    runner = _Runner(
        [],
        by_command={("models", "list"): _rows(), ("models", "discover"): _FOUND},
    )
    app = _app(runner)
    async with app.run_test(size=(170, 46)) as pilot:
        screen = await _config_on_local(pilot, app)
        screen.query_one("#url", Input).focus()
        await pilot.press("enter")
        await _wait_for_models(pilot)
        assert _discovers(runner) == 1, runner.calls
