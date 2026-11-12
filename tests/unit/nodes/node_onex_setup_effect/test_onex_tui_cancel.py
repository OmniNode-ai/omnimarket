# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A slow delegation shows progress and can be cancelled (OMN-23413).

Failure modes:
  F1. A slow model looks like a hang: nothing says the task is still running.
  F2. Esc or Ctrl+C cannot stop a running task, because the app awaits it on
      the input handler and stops reading keys.
  F3. Ctrl+C with nothing running does not quit, or with a task running quits
      instead of cancelling it.
  F4. A second task starts on top of a running one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from textual.pilot import Pilot

from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _root

pytestmark = pytest.mark.unit


class _SlowRunner:
    """A delegation that never answers until it is cancelled."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.cancelled = 0

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        self.calls.append(argv)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return 0, "", ""

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        return 0

    def in_terminal(self, argv: tuple[str, ...]) -> int:
        return 0


async def _status() -> ModelTuiStatus:
    return ModelTuiStatus(workspace="~/ws", model="slow-27b", gateway=False)


def _status_text(app: OnexTui) -> str:
    return str(app.query_one("#status").render())


def _log(app: OnexTui) -> str:
    return "\n".join(str(line) for line in app.query_one("#output").lines)


async def _ask[T](pilot: Pilot[T], text: str) -> None:
    for key in text:
        await pilot.press("space" if key == " " else key)
    await pilot.press("enter")


@pytest.mark.parametrize("key", ["escape", "ctrl+c"])
async def test_a_running_task_shows_progress_and_the_key_cancels_it(key: str) -> None:
    """F1, F2, F3."""
    runner = _SlowRunner()
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(pilot, "hi")
        await pilot.pause(1.2)
        assert runner.calls == [("delegate", "hi", "--json")]
        assert "esc cancels" in _status_text(app)
        assert "working" in _status_text(app)
        await pilot.press(key)
        await pilot.pause()
        assert runner.cancelled == 1
        assert "cancelled after" in _log(app)
        assert "esc cancels" not in _status_text(app)
        assert app.is_running


async def test_a_second_task_waits_for_the_first() -> None:
    """F4."""
    runner = _SlowRunner()
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(pilot, "one")
        await pilot.pause()
        await _ask(pilot, "two")
        await pilot.pause()
        assert runner.calls == [("delegate", "one", "--json")]
        assert "a task is still running" in _log(app)
        await pilot.press("escape")
        await pilot.pause()


async def test_ctrl_c_with_nothing_running_quits() -> None:
    """F3."""
    app = OnexTui(root=_root, runner=_SlowRunner(), load_status=_status)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("ctrl+c")
        await pilot.pause()
    assert app.return_code == 0


async def test_slash_quit_leaves_the_tui() -> None:
    app = OnexTui(root=_root, runner=_SlowRunner(), load_status=_status)
    async with app.run_test(size=(120, 40)) as pilot:
        for key in "/quit":
            await pilot.press("slash" if key == "/" else key)
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_code == 0
