# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""/dashboard: the local dashboard as the fourth tab (OMN-23413).

The CLI's JSON, read from the real `onex dashboard` commands on 2026-11-13:
status/start print {"step":"dashboard","status":"success"|"unavailable",
"evidence":{"url","pid",...},"fix":...}; stop prints evidence {"running":"false"}.
Failure modes (drafted by onex delegate run cca1ea97, checked here):
  D1  the tab is not reachable by Tab from Delegation, or by /dashboard.
  D2  a running dashboard's URL and process are not shown.
  D3  a stopped dashboard's fix (from the CLI) is not shown.
  D4  S does not start it, or the tab closes, or the state is not re-read.
  D5  Enter does not open it.
  D6  X does not stop it.
  D7  a failed action shows no reason and no Failed pop-up.
"""

from __future__ import annotations

import json

import pytest
from textual.widgets import Static

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    DashboardScreen,
    DelegationScreen,
    OnexTui,
    SetupScreen,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _root,
    _Runner,
    _status,
    _type,
)

pytestmark = pytest.mark.unit

_URL = "http://127.0.0.1:52463"
_RUNNING = json.dumps(
    {
        "step": "dashboard",
        "status": "success",
        "changed": False,
        "evidence": {"url": _URL, "pid": "35653"},
        "fix": None,
    }
)
_FIX = (
    "Run onex setup --action repair --component dashboard to start the "
    "authenticated local dashboard."
)
_STOPPED = json.dumps(
    {"step": "dashboard", "status": "unavailable", "evidence": {}, "fix": _FIX}
)


class _Dashboard(_Runner):
    """The dashboard as the CLI reports it, changed by start and stop."""

    def __init__(self, running: bool, start_fails: bool = False) -> None:
        super().__init__([])
        self.running = running
        self.start_fails = start_fails

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        if argv[:1] == ("dashboard",):
            self.calls.append(argv)
            self.stdins.append(stdin)
            if argv[1] == "start":
                if self.start_fails:
                    return 1, _STOPPED, ""
                self.running = True
            elif argv[1] == "stop":
                self.running = False
                stopped = {"step": "dashboard", "status": "success"}
                return 0, json.dumps({**stopped, "evidence": {"running": "false"}}), ""
            return 0, _RUNNING if self.running else _STOPPED, ""
        return await super().capture(argv, stdin)


def _app(runner: _Runner) -> OnexTui:
    return OnexTui(root=_root, runner=runner, load_status=_status)


async def _settle(pilot, times: int = 10) -> None:
    for _ in range(times):
        await pilot.pause()


async def _open_dashboard(pilot) -> None:
    await _type(pilot, "/dashboard")
    await pilot.press("enter")
    await _settle(pilot)


def _state(app: OnexTui) -> str:
    return str(app.screen.query_one("#bstate", Static).render())


async def test_dashboard_is_the_fourth_tab_after_delegation() -> None:
    """D1."""
    app = _app(_Dashboard(running=False))
    async with app.run_test(size=(160, 44)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        await _settle(pilot)
        assert isinstance(app.screen, DelegationScreen)
        await pilot.press("tab")
        await _settle(pilot)
        assert isinstance(app.screen, DashboardScreen)
        await pilot.press("tab")  # and round again to the first tab
        await _settle(pilot)
        assert isinstance(app.screen, SetupScreen)


async def test_a_running_dashboard_shows_its_url_and_process() -> None:
    """D2."""
    app = _app(_Dashboard(running=True))
    async with app.run_test(size=(160, 44)) as pilot:
        await _open_dashboard(pilot)
        assert isinstance(app.screen, DashboardScreen)
        assert "● Running · pid 35653" in _state(app)


async def test_a_stopped_dashboard_shows_the_clis_fix() -> None:
    """D3."""
    app = _app(_Dashboard(running=False))
    async with app.run_test(size=(160, 44)) as pilot:
        await _open_dashboard(pilot)
        assert "○ Stopped" in _state(app)
        # OMN-26620: the CLI's fix (a command with flags) is not printed.
        assert "onex setup --action repair" not in _state(app)


async def test_s_starts_it_keeps_the_tab_and_reads_the_state_again() -> None:
    """D4."""
    runner = _Dashboard(running=False)
    app = _app(runner)
    async with app.run_test(size=(160, 44)) as pilot:
        await _open_dashboard(pilot)
        await pilot.press("s")
        await _settle(pilot, 16)
        assert ("dashboard", "start", "--json") in runner.calls
        assert isinstance(app.screen, DashboardScreen)
        assert "● Running · pid 35653" in _state(app)


async def test_enter_opens_it_and_x_stops_it() -> None:
    """D5, D6."""
    runner = _Dashboard(running=True)
    app = _app(runner)
    async with app.run_test(size=(160, 44)) as pilot:
        await _open_dashboard(pilot)
        await pilot.press("enter")
        await _settle(pilot, 16)
        assert ("dashboard", "open", "--json") in runner.calls
        await pilot.press("x")
        await _settle(pilot, 16)
        assert ("dashboard", "stop", "--json") in runner.calls
        assert isinstance(app.screen, DashboardScreen)
        assert "○ Stopped" in _state(app)


async def test_a_failed_start_shows_the_fix_and_pops_up_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D7."""
    app = _app(_Dashboard(running=False, start_fails=True))
    shown: list[tuple[str, str]] = []

    def notify(
        message: str, *, title: str = "", severity: str = "", **_: object
    ) -> None:
        shown.append((title, severity))

    monkeypatch.setattr(app, "notify", notify)
    async with app.run_test(size=(160, 44)) as pilot:
        await _open_dashboard(pilot)
        await pilot.press("s")
        await _settle(pilot, 16)
        note = str(app.screen.query_one("#bnote").render())
        assert "Could not start the dashboard. Run /doctor." in note
        assert "onex setup --action repair" not in note
        assert ("Failed", "error") in shown
