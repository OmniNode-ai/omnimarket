"""OMN-26620: the Dashboard tab does not sit on "checking…" while the CLI starts.

Failure mode (demo 2026-11-25): each open ran `onex dashboard status`, which takes about
2.8 s, so the tab showed "checking…" for seconds. The state is now read when the app
starts, and a later open shows the last state at once while it reads again.
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

_RUNNING = json.dumps(
    {"status": "success", "evidence": {"running": True, "url": "http://localhost:3000"}}
)


class _SlowCli(HandlerOnexCommand):
    """The real runner's type, with the CLI's slow start and no subprocess."""

    def __init__(self) -> None:
        super().__init__()
        self.status_reads = 0

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        if argv[:2] == ("dashboard", "status"):
            self.status_reads += 1
            await asyncio.sleep(1.0)
            return 0, _RUNNING, ""
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
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=5)


def _state(app: OnexTui) -> str:
    return str(app.screen.query_one("#bstate").render())


async def test_the_dashboard_state_is_ready_when_the_tab_first_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SlowCli()
    monkeypatch.setattr(cli_tui, "HandlerOnexCommand", lambda: runner)
    app = OnexTui(load_status=_ready)  # the app makes its own runner
    async with app.run_test(size=(140, 46)) as pilot:
        await asyncio.sleep(1.3)  # the user reads the welcome; the read finishes
        for key in "/dashboard":
            await pilot.press("slash" if key == "/" else key)
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        assert "checking" not in _state(app), _state(app)
        assert runner.status_reads == 1


async def test_a_second_open_shows_the_last_state_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _SlowCli()
    monkeypatch.setattr(cli_tui, "HandlerOnexCommand", lambda: runner)
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 46)) as pilot:
        await asyncio.sleep(1.3)
        for _ in range(2):
            for key in "/dashboard":
                await pilot.press("slash" if key == "/" else key)
            await pilot.press("enter")
            for _ in range(6):
                await pilot.pause()
            assert "checking" not in _state(app), _state(app)
            await pilot.press("escape")
            for _ in range(3):
                await pilot.pause()
