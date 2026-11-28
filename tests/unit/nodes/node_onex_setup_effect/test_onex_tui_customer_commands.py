# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The TUI's `/` palette lists only the commands a customer needs (OMN-26610).

Driven headless against the real `onex` command tree, not a fixture root.
Failure modes:

  C1  `/` lists every CLI command (hundreds of team-internal ones) instead of
      the customer list, or lists the nine in another order, or the list is
      kept somewhere other than tui_design.yaml (AC1).
  C2  A typed command the TUI does not list still runs, or is told "no match"
      as if it did not exist, or says something other than the one sentence (AC2).
  C3  The TUI honours a list-everything switch: ONEX_CLI_SHOW_ALL_COMMANDS=1
      lists more than the nine, or lets a typed hidden command run (AC3).
  C4  /doctor or /metering is listed but does not run its CLI command and show
      what it printed in the TUI (AC4).
"""

from __future__ import annotations

import pytest
from omnibase_core.cli.cli_commands import cli

from omnimarket.nodes.node_onex_setup_effect.cli_tui import OnexTui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
    tui_design,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _commands,
    _log_text,
    _Runner,
    _status,
    _type,
)

pytestmark = pytest.mark.unit

_SWITCH = "ONEX_CLI_SHOW_ALL_COMMANDS"

#: What the operator chose (OMN-26610), in the order a customer sees them.
_CUSTOMER = [
    "/config",
    "/model",
    "/delegation",
    "/dashboard",
    "/docker",
    "/doctor",
    "/metering",
    "/clear",
    "/help",
    "/quit",
]


@pytest.fixture(autouse=True)
def _customer_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts as a customer; the switch test turns it on itself."""
    monkeypatch.delenv(_SWITCH, raising=False)


def _app(runner: _Runner) -> OnexTui:
    return OnexTui(root=cli, runner=runner, load_status=_status)


def _palette(app: OnexTui) -> list[str]:
    options = app.query_one("#palette").options
    return [str(option.prompt).split()[0] for option in options]


def test_banner_tip_does_not_promise_every_command() -> None:
    """C1: the welcome banner must not say 'every command' when only some are listed."""
    slash_tips = [t for t in tui_design().banner.tips if t.key == "/"]
    assert slash_tips, "no banner tip with key '/'"
    assert "every command" not in slash_tips[0].text


async def test_slash_lists_exactly_the_customer_commands_in_order() -> None:
    """C1, AC1."""
    app = _app(_Runner([]))
    async with app.run_test() as pilot:
        await pilot.press("slash")
        assert _palette(app) == _CUSTOMER


async def test_the_customer_list_is_declared_once_in_tui_design_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1, AC1: the list is data in the design file, not a list in code."""
    from omnimarket.nodes.node_onex_setup_effect import cli_tui

    declared = ["/" + " ".join(path) for path in tui_design().customer_commands]
    assert declared == _CUSTOMER

    patched = tui_design().model_copy(
        update={"customer_commands": (("config",), ("quit",))}
    )
    monkeypatch.setattr(cli_tui, "tui_design", lambda: patched)
    assert cli_tui._is_customer_command(("config",))
    assert cli_tui._is_customer_command(("config", "init"))
    assert not cli_tui._is_customer_command(("delegation",))

    app = _app(_Runner([]))
    async with app.run_test() as pilot:
        await pilot.press("slash")
        assert _palette(app) == ["/config", "/quit"]


async def test_a_typed_hidden_command_runs_nothing_and_says_so() -> None:
    """C2, AC2."""
    runner = _Runner(["this must never be printed"])
    app = _app(runner)
    async with app.run_test() as pilot:
        await _type(pilot, "/models list")
        await pilot.press("enter")
        await pilot.pause()
        assert _commands(runner) == []
        assert runner.terminal_calls == []
        assert "Not a TUI command: run onex models list in a terminal" in _log_text(app)
        assert "this must never be printed" not in _log_text(app)


async def test_a_command_that_does_not_exist_is_still_no_match() -> None:
    """C2: the hidden sentence is for real commands only."""
    app = _app(_Runner([]))
    async with app.run_test() as pilot:
        await _type(pilot, "/nosuchcommand")
        await pilot.press("enter")
        await pilot.pause()
        assert "Not a TUI command" not in _log_text(app)
        assert "no onex command matches '/nosuchcommand'" in _log_text(app)


async def test_no_environment_switch_widens_the_list_or_runs_hidden_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3, AC3: the TUI has no list-everything switch, so setting the old one changes nothing."""
    monkeypatch.setenv(_SWITCH, "1")
    runner = _Runner(["this must never be printed"])
    app = _app(runner)
    async with app.run_test() as pilot:
        await pilot.press("slash")
        assert _palette(app) == _CUSTOMER
    app = _app(runner)
    async with app.run_test() as pilot:
        await _type(pilot, "/models connections")
        await pilot.press("enter")
        await pilot.pause()
        assert _commands(runner) == []
        assert "Not a TUI command: run onex models connections in a terminal" in (
            _log_text(app)
        )


@pytest.mark.parametrize("name", ["doctor", "metering"])
async def test_doctor_and_metering_run_and_show_their_output(name: str) -> None:
    """C4, AC4: through the real command tree, in the customer view."""
    runner = _Runner([f"{name} output line"])
    app = _app(runner)
    async with app.run_test() as pilot:
        await pilot.press("slash")
        assert f"/{name}" in _palette(app)
        await _type(pilot, name)
        await pilot.press("enter")
        await pilot.pause()
        assert _commands(runner) == [(name,)]
        assert f"{name} output line" in _log_text(app)
        assert "Not a TUI command" not in _log_text(app)


@pytest.mark.parametrize(
    "typed",
    ["/delegation on", "/delegation off", "/config init"],
)
async def test_typed_subcommands_of_a_customer_command_still_run(typed: str) -> None:
    """C2, AC2: the sentence is for commands outside the nine, not for their subcommands."""
    runner = _Runner(["subcommand ran"])
    app = _app(runner)
    async with app.run_test() as pilot:
        await _type(pilot, typed)
        await pilot.press("enter")
        await pilot.pause()
        assert "Not a TUI command" not in _log_text(app)
        assert _commands(runner) == [tuple(typed.lstrip("/").split())]
        assert "subcommand ran" in _log_text(app)
