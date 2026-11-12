# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A key typed into the TUI is never shown, and every action works by keyboard (OMN-23413 AC15).

Failure modes:
  F1. A key typed into /config appears in the rendered screen or the output pane.
  F2. The scan cannot see text at all, so a clean result proves nothing
      (positive control: a planted copy is found).
  F3. At 80x24 the form's buttons are unreachable, or an action needs a mouse.
"""

from __future__ import annotations

import json

import pytest
from textual.pilot import Pilot

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    DelegationScreen,
    ModelPickScreen,
    ModelTuiStatus,
    OnexTui,
    SetupScreen,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _root, _Runner

pytestmark = pytest.mark.unit

_SENTINEL = "sk-or-sentinel-ac15-7f3a9c41"


async def _status() -> ModelTuiStatus:
    return ModelTuiStatus(workspace="~/ws", model="m", gateway=False)


def _app(runner: _Runner) -> OnexTui:
    return OnexTui(root=_root, runner=runner, load_status=_status)


def _screen_text(app: OnexTui) -> str:
    return app.export_screenshot()


def _pane_text(app: OnexTui) -> str:
    return "\n".join(str(line) for line in app.query_one("#output").lines)


async def _type[T](pilot: Pilot[T], text: str) -> None:
    names = {" ": "space", "/": "slash", "-": "minus"}
    for key in text:
        await pilot.press(names.get(key, key))


async def _focus[T](
    pilot: Pilot[T], app: OnexTui, widget_id: str, limit: int = 20
) -> None:
    """Tab until ``widget_id`` has focus: proves it is reachable by keyboard."""
    for _ in range(limit):
        if app.focused is not None and app.focused.id == widget_id:
            return
        await pilot.press("tab")
    raise AssertionError(f"#{widget_id} is not reachable with Tab")


async def test_a_typed_key_never_reaches_the_screen_or_the_pane() -> None:
    """F1, F2, F3: typed by keyboard at 80x24, saved by keyboard, never shown."""
    report = json.dumps({"observations": [{"step": "models", "status": "success"}]})
    runner = _Runner([], by_command={("setup", "--no-interactive"): report})
    app = _app(runner)
    async with app.run_test(size=(80, 24)) as pilot:
        await _type(pilot, "/config")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        await _focus(pilot, app, "provider")
        await pilot.press("enter")
        await pilot.pause()
        # Gemini, Ollama (selected), OpenAI, OpenRouter: two steps down; the
        # list wraps, so more presses would come back round.
        await pilot.press("down", "down")
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#provider").value == "openrouter"
        await _focus(pilot, app, "key")
        await _type(pilot, _SENTINEL)
        await _focus(pilot, app, "model")
        await _type(pilot, "qwen/qwen3")
        await _focus(pilot, app, "save")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        setups = [call for call in runner.calls if call[0] == "setup"]
        assert setups, "save by keyboard did not run setup"
        assert runner.stdins[runner.calls.index(setups[0])] == _SENTINEL + "\n"
        assert all(_SENTINEL not in " ".join(call) for call in runner.calls)
        assert _SENTINEL not in _screen_text(app)
        assert _SENTINEL not in _pane_text(app)

        # F2: the same scans find a planted copy.
        app.query_one("#output").write(f"planted {_SENTINEL}")
        await pilot.pause()
        assert _SENTINEL in _pane_text(app)
        assert _SENTINEL in _screen_text(app)


async def test_every_shortcut_opens_and_closes_by_keyboard_at_80_by_24() -> None:
    """F3: /model, /delegation and /config open from the keyboard and Esc closes them."""
    rows = json.dumps({"provider": "ollama", "set_up": True, "models": ["a"]})
    app = _app(_Runner([], by_command={("models", "list"): rows}))
    async with app.run_test(size=(80, 24)) as pilot:
        for command, screen in (
            ("/config", SetupScreen),
            ("/model", ModelPickScreen),
            ("/delegation", DelegationScreen),
        ):
            await _type(pilot, command)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, screen), command
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, screen), command
        await _type(pilot, "/quit")
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_code == 0
