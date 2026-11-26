"""OMN-26620: what the end-to-end pass in a real terminal found (2026-11-26).

E1  Esc on the / palette closes it but leaves the "/" in the prompt, so the
    next command typed reads "//help" and is refused.
E2  "exit" or "quit" typed without a slash is sent to a model as a task.
E3  After Start, the Dashboard tab still says "Stopped" with an empty note for
    seconds (it reads the state again although the start reply carries it),
    so a working start looks like a failure; and it never says it started.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    DashboardScreen,
    ModelTuiStatus,
    OnexTui,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_command import (
    HandlerOnexCommand,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _log_text,
    _Runner,
    _type,
)

pytestmark = pytest.mark.timeout(60)


async def test_esc_on_the_palette_clears_a_lone_slash() -> None:
    """E1."""
    app = _app(_Runner([]))
    async with app.run_test(size=(120, 40)) as pilot:
        await _type(pilot, "/")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#prompt").value == ""
        await _type(pilot, "/help")
        await pilot.press("enter")
        for _ in range(4):
            await pilot.pause()
        assert "no onex command matches" not in _log_text(app), _log_text(app)


@pytest.mark.parametrize("word", ["exit", "quit", "Exit"])
async def test_exit_or_quit_alone_quits(word: str) -> None:
    """E2."""
    runner = _Runner([])
    app = _app(runner)
    async with app.run_test(size=(120, 40)) as pilot:
        await _type(pilot, word)
        await pilot.press("enter")
        for _ in range(4):
            await pilot.pause()
        assert not any(call[:1] == ("delegate",) for call in runner.calls), runner.calls
    assert app.return_code == 0


_STOPPED = json.dumps({"status": "success", "evidence": {}})
_STARTED = json.dumps(
    {
        "status": "success",
        "evidence": {"url": "http://127.0.0.1:40627", "pid": "4821"},
    }
)


class _Cli(HandlerOnexCommand):
    """Start answers at once; reading the state again is slow, as it is live."""

    def __init__(self) -> None:
        super().__init__()
        self.running = False

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        if argv[:2] == ("dashboard", "start"):
            self.running = True
            return 0, _STARTED, ""
        if argv[:2] == ("dashboard", "status"):
            await asyncio.sleep(3.0)
            return 0, _STARTED if self.running else _STOPPED, ""
        return 0, "{}", ""

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        emit("{}")
        return 0


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=3)


async def test_start_shows_running_and_says_so_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E3."""
    runner = _Cli()
    monkeypatch.setattr(cli_tui, "HandlerOnexCommand", lambda: runner)
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 46)) as pilot:
        await asyncio.sleep(3.3)  # the startup read lands: stopped
        await _type(pilot, "/dashboard")
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        await pilot.press("s")
        for _ in range(8):
            await pilot.pause(0.05)
        state = str(app.screen.query_one("#bstate").render())
        note = str(app.screen.query_one("#bnote").render())
        assert "Running" in state, state
        assert "4821" in state, state
        assert "running" in note.lower(), note
