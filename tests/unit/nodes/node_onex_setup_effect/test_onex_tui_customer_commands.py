# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The onex TUI's `/` list shows the commands a customer needs, driven headless.

Failure modes:
  F1. `/` lists every CLI command, so a customer sees internal team tools
      (AC1).
  F2. A typed internal command runs instead of saying it is not a TUI command
      (AC2).
  F3. The operator switch ONEX_CLI_SHOW_ALL_COMMANDS=1 no longer lists every
      command (AC3).
  F4. /doctor or /metering is missing from the list or does not run its CLI
      command in the TUI (AC4).
"""

from __future__ import annotations

from collections.abc import Callable

import click
import pytest
from textual.pilot import Pilot

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
)

pytestmark = pytest.mark.unit

#: Typing `/` lists these, in this order (OMN-26610 AC1).
_CUSTOMER = (
    "/config",
    "/model",
    "/delegation",
    "/dashboard",
    "/doctor",
    "/metering",
    "/clear",
    "/help",
    "/quit",
)


@click.group()
def _root() -> None:
    """Fixture root."""


@_root.command("doctor")
def _doctor() -> None:
    """Run diagnostic checks."""


@_root.command("metering")
def _metering() -> None:
    """Read delegation metering."""


@_root.command("dashboard")
def _dashboard() -> None:
    """Dashboard."""


@_root.command("setup")
def _setup() -> None:
    """Set up ONEX."""


@_root.group("scm")
def _scm() -> None:
    """Internal source control tools."""


@_scm.command("pr-list")
def _pr_list() -> None:
    """List pull requests."""


@_root.group("auth")
def _auth() -> None:
    """Internal auth tools."""


@_auth.command("lane-login")
def _lane_login() -> None:
    """Log a lane in."""


@_root.group("models")
def _models() -> None:
    """Model servers."""


@_models.command("connections")
def _connections() -> None:
    """List named model connections."""


#: The setup read the TUI starts with, which these tests leave out.
_SETUP_READS = {("models", "list", "--json"), ("models", "connections", "--json")}


class _Runner:
    def __init__(self) -> None:
        self._all: list[tuple[str, ...]] = []
        self.terminal_calls: list[tuple[str, ...]] = []

    @property
    def calls(self) -> list[tuple[str, ...]]:
        return [c for c in self._all if c not in _SETUP_READS]

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        self._all.append(argv)
        return 0, "", ""

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        self._all.append(argv)
        emit(f"output of {' '.join(argv)}")
        return 0

    def in_terminal(self, argv: tuple[str, ...]) -> int:
        self.terminal_calls.append(argv)
        return 0


async def _status() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off")


def _app(runner: _Runner) -> OnexTui:
    return OnexTui(root=_root, runner=runner, load_status=_status)


async def _type[T](pilot: Pilot[T], text: str) -> None:
    names = {" ": "space", "/": "slash", "-": "minus"}
    for key in text:
        await pilot.press(names.get(key, key))


def _listed(app: OnexTui) -> list[str]:
    return [f"/{o.id}" for o in app.query_one("#palette").options]


def _log_text(app: OnexTui) -> str:
    return "\n".join(str(line) for line in app.query_one("#output").lines)


@pytest.fixture(autouse=True)
def _customer_view(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_CLI_SHOW_ALL_COMMANDS", raising=False)


async def test_slash_lists_exactly_the_customer_commands_in_order() -> None:
    """AC1 / F1."""
    app = _app(_Runner())
    async with app.run_test() as pilot:
        await pilot.press("slash")
        assert tuple(_listed(app)) == _CUSTOMER


async def test_a_typed_internal_command_runs_nothing_and_says_where_to_run_it() -> None:
    """AC2 / F2."""
    runner = _Runner()
    app = _app(runner)
    async with app.run_test() as pilot:
        for typed in ("/scm pr-list", "/auth lane-login"):
            await _type(pilot, typed)
            await pilot.press("enter")
            await pilot.pause()
        text = _log_text(app)
        assert "Not a TUI command: run onex scm pr-list in a terminal" in text
        assert "Not a TUI command: run onex auth lane-login in a terminal" in text
        assert runner.calls == []
        assert runner.terminal_calls == []


async def test_no_environment_switch_widens_the_list_or_runs_hidden_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 / F3: setting the old switch changes nothing."""
    monkeypatch.setenv("ONEX_CLI_SHOW_ALL_COMMANDS", "1")
    runner = _Runner()
    app = _app(runner)
    async with app.run_test() as pilot:
        await pilot.press("slash")
        assert tuple(_listed(app)) == _CUSTOMER
    app = _app(runner)
    async with app.run_test() as pilot:
        await _type(pilot, "/scm pr-list")
        await pilot.press("enter")
        await pilot.pause()
        assert runner.calls == []
        assert "Not a TUI command: run onex scm pr-list in a terminal" in _log_text(app)


@click.group()
def _bare_root() -> None:
    """A root that registers neither doctor nor metering."""


@pytest.mark.parametrize("command", ["doctor", "metering"])
async def test_doctor_and_metering_run_their_cli_command_and_show_output(
    command: str,
) -> None:
    """AC4 / F4: the entries are the design file's, not whatever the CLI lists."""
    runner = _Runner()
    app = OnexTui(root=_bare_root, runner=runner, load_status=_status)
    async with app.run_test() as pilot:
        await _type(pilot, f"/{command}")
        await pilot.press("enter")
        await pilot.pause()
        assert runner.calls == [(command,)]
        assert f"output of {command}" in _log_text(app)
