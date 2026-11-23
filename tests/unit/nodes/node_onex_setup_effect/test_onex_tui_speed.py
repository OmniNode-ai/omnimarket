# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The onex TUI answers every action quickly and always shows it is working.

Measured on dev with the real runner and a throwaway HOME, each start of a new
`onex` program took 2.3 to 3.2 s before /config, /model, /delegation or
/dashboard showed, and 5 s or more until the tab held its data.

Failure modes:
  S1. A TUI action starts a new `onex` program instead of running in the
      running TUI (AC1).
  S2. /config, /model, /delegation or /dashboard takes 1 second or more to
      show and fill (AC1).
  S3. An action that waits shows no spinner and no step name within 0.2 s, or
      Esc does not cancel it (AC2).
  S4. /model waits for a slow provider before it shows any table (AC3).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

from omnimarket.handlers.model_catalog import ModelsDevIndex
from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_model_table import ModelTable
from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    DashboardScreen,
    ModelPickScreen,
    ModelTuiStatus,
    OnexTui,
    _TabbedScreen,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_command import (
    HandlerOnexCommand,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _index,
    _root,
    _selected_rows,
)

pytestmark = pytest.mark.unit

_TABS = ("config", "model", "delegation", "dashboard")
#: The frames the live line draws from (the design file's, not a copy).
_FRAMES = tui_design().activity.frames


@pytest.fixture(autouse=True)
def _clean_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A throwaway HOME, and no network for models.dev or a model server."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli_tui, "_default_index", _index)
    monkeypatch.setattr(cli_tui, "_default_read_server", lambda _base, _models: {})


async def _type[T](pilot: Pilot[T], text: str) -> None:
    names = {" ": "space", "/": "slash", "-": "minus"}
    for key in text:
        await pilot.press(names.get(key, key))


class _Spy:
    """Counts the `onex` programs the real runner starts."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.started: list[tuple[str, ...]] = []
        real = asyncio.create_subprocess_exec

        async def spy(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
            self.started.append(args)
            return await real(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)


class _Counting(HandlerOnexCommand):
    """The real runner, counting the commands in flight."""

    def __init__(self) -> None:
        super().__init__()
        self.active = 0

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        self.active += 1
        try:
            return await super().stream(argv, emit, stdin)
        finally:
            self.active -= 1

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        self.active += 1
        try:
            return await super().capture(argv, stdin)
        finally:
            self.active -= 1


async def _open(name: str, runner: _Counting) -> tuple[float, float]:
    """Seconds from Enter until the tab shows, and until it holds its data, with
    the command typed as soon as the TUI is up (the worst case)."""
    app = OnexTui(root=_root, runner=runner)
    async with app.run_test(size=(140, 45)) as pilot:
        await _type(pilot, f"/{name}")
        start = time.monotonic()
        await pilot.press("enter")
        shown = 0.0
        while time.monotonic() - start < 30:
            await pilot.pause(0.02)
            now = time.monotonic() - start
            if not shown and isinstance(app.screen, _TabbedScreen):
                shown = now
            if shown and runner.active == 0:
                return shown, now
        return shown, 30.0


@pytest.mark.parametrize("name", _TABS)
async def test_a_tab_runs_its_commands_in_the_running_tui(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1 / S1: no new onex program for any read the four tabs make."""
    spy = _Spy(monkeypatch)
    await _open(name, _Counting())
    assert spy.started == []


@pytest.mark.parametrize("name", _TABS)
async def test_a_tab_opens_in_under_one_second(name: str) -> None:
    """AC1 / S2: shown and filled, from Enter, over 2 s on dev."""
    shown, settled = await _open(name, _Counting())
    assert shown < 1.0, f"/{name} showed after {shown:.2f}s"
    assert settled < 1.0, f"/{name} held its data after {settled:.2f}s"


# AC2 ------------------------------------------------------------------------


async def _status() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off")


class _Slow:
    """A runner whose commands take ``seconds``; the rest answer at once."""

    def __init__(
        self,
        slow: Callable[[tuple[str, ...]], bool],
        seconds: float = 30,
        by_command: dict[tuple[str, ...], str] | None = None,
    ) -> None:
        self._slow = slow
        self._seconds = seconds
        self._by_command = by_command or {}
        self.calls: list[tuple[str, ...]] = []
        self.cancelled: list[tuple[str, ...]] = []

    async def _answer(self, argv: tuple[str, ...]) -> str:
        self.calls.append(argv)
        if self._slow(argv):
            try:
                await asyncio.sleep(self._seconds)
            except asyncio.CancelledError:
                self.cancelled.append(argv)
                raise
        return self._by_command.get(argv[:2], "")

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        return 0, await self._answer(argv), ""

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        for line in (await self._answer(argv)).splitlines():
            emit(line)
        return 0

    def in_terminal(self, argv: tuple[str, ...]) -> int:
        return 0


def _live_line(app: OnexTui) -> str:
    """The live line under the log, empty while it is hidden."""
    live = app.screen_stack[0].query_one("#activity", Static)
    return str(live.render()) if live.display else ""


def _tab_line(app: OnexTui, note: str) -> str:
    return str(app.screen.query_one(note, Static).render())


async def _wait_for[T](
    pilot: Pilot[T], seen: Callable[[], bool], seconds: float
) -> float | None:
    """Seconds until ``seen`` holds, or None if it does not within ``seconds``."""
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        if seen():
            return time.monotonic() - start
        await pilot.pause(0.01)
    return None


def _spinning(line: str, step: str) -> bool:
    return any(frame in line for frame in _FRAMES) and step in line


async def test_a_slow_command_shows_a_spinner_and_its_step_then_esc_cancels() -> None:
    """AC2 / S3: a palette command that waits."""
    runner = _Slow(lambda argv: argv[:1] == ("doctor",))
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(140, 45)) as pilot:
        await _type(pilot, "/doctor")
        await pilot.press("enter")
        took = await _wait_for(pilot, lambda: _spinning(_live_line(app), "doctor"), 0.2)
        assert took is not None, f"no spinner with the step name: {_live_line(app)!r}"
        assert "esc to cancel" in _live_line(app)
        await pilot.press("escape")
        await pilot.pause()
        assert runner.cancelled == [("doctor",)]
        assert _live_line(app) == ""


async def test_opening_a_tab_that_waits_shows_a_spinner_and_esc_cancels() -> None:
    """AC2 / S3: the first read of the setup is slow."""
    runner = _Slow(lambda argv: argv[:2] == ("models", "list"))
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(140, 45)) as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        took = await _wait_for(pilot, lambda: _spinning(_live_line(app), "Model"), 0.2)
        assert took is not None, f"no spinner with the step name: {_live_line(app)!r}"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, _TabbedScreen)
        assert _live_line(app) == ""


async def test_a_tab_action_that_waits_shows_a_spinner_and_esc_cancels() -> None:
    """AC2 / S3: starting the dashboard from its tab."""
    runner = _Slow(lambda argv: argv[:2] == ("dashboard", "start"))
    app = OnexTui(root=_root, runner=runner, load_status=_status)
    async with app.run_test(size=(140, 45)) as pilot:
        await _type(pilot, "/dashboard")
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert isinstance(app.screen, DashboardScreen)
        await pilot.press("s")
        took = await _wait_for(
            pilot, lambda: _spinning(_tab_line(app, "#bnote"), "start"), 0.2
        )
        assert took is not None, f"no spinner: {_tab_line(app, '#bnote')!r}"
        assert "esc to cancel" in _tab_line(app, "#bnote")
        await pilot.press("escape")
        await pilot.pause()
        assert runner.cancelled == [("dashboard", "start", "--json")]
        assert "esc to cancel" not in _tab_line(app, "#bnote")
        assert isinstance(app.screen, DashboardScreen)  # Esc stopped the action only


# AC3 ------------------------------------------------------------------------


async def test_the_model_list_opens_from_saved_data_and_refreshes_in_the_background() -> (
    None
):
    """AC3 / S4: the live models.dev read takes 3 seconds."""
    slow_seconds = 3.0

    def slow_index() -> ModelsDevIndex:
        time.sleep(slow_seconds)
        return _index()

    runner = _Slow(lambda _argv: False, by_command=_selected_rows(connection=False))
    app = OnexTui(root=_root, runner=runner, load_status=_status, load_index=slow_index)
    async with app.run_test(size=(140, 45)) as pilot:
        await _type(pilot, "/model")
        start = time.monotonic()
        await pilot.press("enter")
        await _wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ModelPickScreen)
                and bool(app.screen.query_one("#models", ModelTable).entries)
            ),
            slow_seconds + 3,
        )
        took = time.monotonic() - start  # Enter is part of the wait
        assert took < 1.0, f"the table showed after {took:.2f}s"
        before = {
            e.model_id: e.input_usd_per_million
            for e in app.screen.query_one("#models", ModelTable).entries
        }
        assert "qwen/qwen3.8-27b" in before
        assert before["qwen/qwen3.8-27b"] is None  # the saved copy has no price yet
        # The refresh lands once the slow read finishes, without a keypress.
        landed = await _wait_for(
            pilot,
            lambda: any(
                e.input_usd_per_million is not None
                for e in app.screen.query_one("#models", ModelTable).entries
            ),
            slow_seconds + 3,
        )
        assert landed is not None, "the background refresh never filled the table"
        assert time.monotonic() - start >= slow_seconds - 0.5
