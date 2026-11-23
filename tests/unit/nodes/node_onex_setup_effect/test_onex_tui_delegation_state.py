# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""/delegation never draws a state it has not read (OMN-23413).

Seen in review 2026-11-15, each one ending with an untouched Save that ran
`onex delegation off` while delegation was on. Failure modes:

  DS1  a tab opened before the first status read lands draws OFF, is not
       redrawn when the read lands, and Save from it sends `delegation off`.
  DS2  `/delegation on|off` (with or without --replace) and `/hooks ...` typed
       in the palette do not read the status again; the bar and the next tab
       keep the state from before.
  DS3  a status read that fails (non-zero exit, no status line, or an
       exception) is drawn as a definite OFF, nothing says why, and an
       untouched Save sends `delegation off`.
  DS4  Save trusts the Claude Code state read when the tab opened, so it stays
       refused after the user installs Claude Code.
  DS5  a failed Save quotes the first failing setup step's fix instead of the
       delegation command's own: a remedy (`onex setup`) the TUI does not
       offer, and for a refused switch-off the wrong step.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import click
import pytest
from textual.pilot import Pilot
from textual.widgets import Input

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _log_text,
    _press,
    _root,
    _Runner,
    _selected_rows,
    _type,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui_delegation_hooks import (
    _state,
    _text,
)

pytestmark = pytest.mark.unit

_ON = {"prompt_hook": "installed"}
_OFF = {"prompt_hook": "missing"}
_SWITCHES = {("delegation", "on"), ("delegation", "off")}


def _switches(runner: _Runner) -> list[tuple[str, ...]]:
    """The commands that change delegation, in the order they ran."""
    return [
        call
        for call in runner.calls
        if call[:2] in _SWITCHES
        or call[:2] in {("hooks", "enable"), ("hooks", "disable")}
    ]


def _status_doc(status: str, claude_code: str = "ready") -> str:
    """`onex delegation status --json` as cli_delegation prints it."""
    hooks = _ON if status == "on" else _OFF
    return json.dumps(
        {
            "status": status,
            "claude_code": claude_code,
            "fix": None,
            "observations": [
                {
                    "step": "claude-plugin",
                    "status": "success" if claude_code == "ready" else "unavailable",
                    "fix": None,
                    "evidence": (
                        {} if claude_code == "ready" else {"claude_code": claude_code}
                    ),
                },
                {"step": "claude-hooks", "status": "success", "evidence": hooks},
            ],
        }
    )


class _CodedRunner(_Runner):
    """The fixture runner with an exit code per command (by its first two words)."""

    def __init__(
        self, by_command: dict[tuple[str, ...], str], codes: dict[tuple[str, ...], int]
    ) -> None:
        super().__init__([], by_command=by_command)
        self._codes = codes

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        _, out, err = await super().capture(argv, stdin)
        return self._codes.get(argv[:2], 0), out, err

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        await super().stream(argv, emit, stdin)
        return self._codes.get(argv[:2], 0)


async def _settle(pilot: Pilot[int], times: int = 8) -> None:
    for _ in range(times):
        await pilot.pause()


async def _open_tab(pilot: Pilot[int]) -> None:
    await _type(pilot, "/delegation")
    await pilot.press("enter")
    await _settle(pilot, 6)


def _bar(app: OnexTui) -> str:
    return " ".join(str(app.screen_stack[0].query_one("#status").render()).split())


# DS1 ------------------------------------------------------------------------


async def test_a_tab_opened_before_the_status_read_waits_for_it() -> None:
    """DS1: delegation is on; the tab opens while the start-up read still runs."""
    words = tui_design().delegation
    rows = _selected_rows(connection=False)
    runner = _Runner([], by_command=rows)
    landed = asyncio.Event()

    async def status() -> ModelTuiStatus:
        await landed.wait()  # the real read runs two onex commands, seconds
        return ModelTuiStatus(models=2, delegation="hook", hooks=_ON)

    app = OnexTui(root=_root, runner=runner, load_status=status)
    async with app.run_test(size=(160, 46)) as pilot:
        await _open_tab(pilot)
        before = _text(app, "#on-state")
        await pilot.press("ctrl+s")  # Save with the switch untouched
        await _settle(pilot)
        sent_before = _switches(runner)
        note = _text(app, "#dnote")
        landed.set()
        await _settle(pilot)
        after = _text(app, "#on-state")
        switch = app.screen.any_on()
        prompt = _state(_text(app, "#hook-prompt_hook"))
    assert words.state_off not in before, before
    assert words.state_reading in before
    assert sent_before == [], sent_before
    assert words.save_reading in note
    assert switch is True
    assert words.state_on in after
    assert words.state_pending not in after
    assert prompt == words.hook_installed


# DS2 ------------------------------------------------------------------------


@click.group()
def _droot() -> None:
    """Fixture root with the delegation and hooks groups."""


@_droot.group("delegation")
def _delegation() -> None:
    """Claude Code delegation."""


@_delegation.command("on")
@click.option("--replace", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def _on(replace: bool, json_output: bool) -> None:
    """Switch delegation on."""


@_delegation.command("off")
@click.option("--json", "json_output", is_flag=True)
def _off(json_output: bool) -> None:
    """Switch delegation off."""


@_droot.group("hooks")
def _hooks() -> None:
    """Claude Code hooks."""


@_hooks.command("enable")
@click.argument("name")
def _enable(name: str) -> None:
    """Enable a hook."""


@_hooks.command("disable")
@click.argument("name")
def _disable(name: str) -> None:
    """Disable a hook."""


@pytest.mark.parametrize(
    ("typed", "now_on"),
    [
        ("/delegation on", True),
        ("/delegation on --replace", True),
        ("/delegation off", False),
        ("/hooks enable POST_SKILL_DELEGATION_ENFORCER", True),
        ("/hooks disable POST_SKILL_DELEGATION_ENFORCER", False),
    ],
)
async def test_a_palette_command_that_switches_delegation_reads_it_again(
    typed: str, now_on: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DS2: the bar and a tab opened afterwards show the state the command left."""
    monkeypatch.setenv(
        "ONEX_CLI_SHOW_ALL_COMMANDS", "1"
    )  # OMN-26610: the operator view
    runner = _Runner([], by_command=_selected_rows(connection=False))
    reads = 0

    async def status() -> ModelTuiStatus:
        nonlocal reads
        reads += 1
        ran = [c[:2] for c in runner.calls if c[:1] in (("delegation",), ("hooks",))]
        on = ran[-1][1] in ("on", "enable") if ran else not now_on
        return ModelTuiStatus(
            models=2, delegation="hook" if on else "off", hooks=_ON if on else _OFF
        )

    app = OnexTui(root=_droot, runner=runner, load_status=status)
    async with app.run_test(size=(160, 46)) as pilot:
        await _settle(pilot, 4)
        first = reads
        app.query_one("#prompt", Input).value = typed  # as typed, without the wait
        await pilot.press("enter")
        await _settle(pilot)
        bar = _bar(app)
        await _open_tab(pilot)
        switch = app.screen.any_on()
    word = tui_design().words["delegation_hook" if now_on else "delegation_off"]
    assert reads > first, "the status was not read again"
    assert word in bar, bar
    assert switch is now_on


def test_no_status_refresh_is_keyed_on_the_removed_gateway_group() -> None:
    """DS2: the gateway's `claude` command group is gone."""
    assert ("claude",) not in cli_tui._STATE_GROUPS
    assert {("delegation",), ("hooks",), ("models",)} <= set(cli_tui._STATE_GROUPS)


# DS3 ------------------------------------------------------------------------


class _FailingRead:
    """HandlerOnexCommand for load_status: models list works, status fails."""

    def __init__(self, status_lines: list[str], code: int) -> None:
        self._status_lines = status_lines
        self._code = code

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        if argv[:2] == ("delegation", "status"):
            for line in self._status_lines:
                emit(line)
            return self._code
        if argv[:2] == ("models", "connections"):
            emit(json.dumps({"status": "ready", "connections": []}))
            return 0
        # This case exercises a failed delegation status read, after a valid
        # models-list response. The explorer fixture also has non-wire rows.
        emit(
            json.dumps(
                {
                    "provider": "openrouter",
                    "set_up": True,
                    "models": ["qwen/qwen3.8-27b"],
                }
            )
        )
        return 0


_FAILED_READS = {
    "non-zero exit": (["re.PatternError: nothing to repeat at position 0"], 1),
    "no status line": (["Claude Code delegation: on"], 0),
    "exit 1 with a status line": ([_status_doc("off")], 1),
}


@pytest.mark.parametrize("case", list(_FAILED_READS))
async def test_a_failed_status_read_is_unknown_not_off(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DS3: load_status itself says the read failed, and why."""
    lines, code = _FAILED_READS[case]
    monkeypatch.setattr(
        cli_tui, "HandlerOnexCommand", lambda: _FailingRead(lines, code)
    )
    status = await cli_tui.load_status()
    assert status.delegation == "unknown"
    assert status.hooks == {}
    assert status.problem


@pytest.mark.parametrize("case", ["non-zero exit", "exception"])
async def test_a_tab_after_a_failed_read_says_so_and_save_sends_nothing(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DS3: the bar, the log and the tab say the state was not read."""
    words = tui_design().delegation
    if case == "exception":

        async def status() -> ModelTuiStatus:
            raise RuntimeError("onex was not found")

        loader: Callable[[], Awaitable[ModelTuiStatus]] | None = status
    else:
        lines, code = _FAILED_READS[case]
        monkeypatch.setattr(
            cli_tui, "HandlerOnexCommand", lambda: _FailingRead(lines, code)
        )
        loader = None  # the real load_status
    runner = _Runner([], by_command=_selected_rows(connection=False))
    app = OnexTui(root=_root, runner=runner, load_status=loader)
    async with app.run_test(size=(160, 46)) as pilot:
        await _settle(pilot, 4)
        bar = _bar(app)
        log = _log_text(app)
        await _open_tab(pilot)
        label = _text(app, "#on-state")
        row = _text(app, "#hook-prompt_hook")
        await pilot.press("ctrl+s")
        await _settle(pilot)
        note = _text(app, "#dnote")
    assert tui_design().words["delegation_off"] not in bar, bar
    assert tui_design().words["delegation_unknown"] in bar
    assert ("onex was not found" if case == "exception" else "exit 1") in log
    assert words.state_off not in label, label
    assert words.state_unread in label
    assert _state(row) == words.hook_unread
    assert _switches(runner) == []
    assert words.save_unread in note


async def test_a_flipped_switch_after_a_failed_read_still_switches_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DS3: the user's own choice is sent; only an untouched switch sends nothing."""
    lines, code = _FAILED_READS["non-zero exit"]
    monkeypatch.setattr(
        cli_tui, "HandlerOnexCommand", lambda: _FailingRead(lines, code)
    )
    rows = _selected_rows(connection=False)
    rows[("delegation", "on")] = json.dumps({"status": "on"})
    runner = _Runner([], by_command=rows)
    app = OnexTui(root=_root, runner=runner, load_status=None)
    async with app.run_test(size=(160, 46)) as pilot:
        await _settle(pilot, 4)
        await _open_tab(pilot)
        app.screen.set_all(True)
        await pilot.pause()
        label = _text(app, "#on-state")
        await pilot.press("ctrl+s")
        await _settle(pilot)
    assert tui_design().delegation.state_pending in label
    assert _switches(runner) == [("delegation", "on", "--json")]


# DS4 ------------------------------------------------------------------------


async def test_save_checks_claude_code_again() -> None:
    """DS4: Claude Code installed after the tab opened lets the next Save run."""
    rows = _selected_rows(connection=False)
    rows[("delegation", "status")] = _status_doc("off", claude_code="not_installed")
    rows[("delegation", "on")] = json.dumps({"status": "on"})
    runner = _Runner([], by_command=rows)

    async def status() -> ModelTuiStatus:
        return ModelTuiStatus(models=2, delegation="off", hooks=_OFF)

    app = OnexTui(root=_root, runner=runner, load_status=status)
    async with app.run_test(size=(160, 46)) as pilot:
        await _settle(pilot, 4)
        await _open_tab(pilot)
        app.screen.set_all(True)
        await _press(pilot, app, "dsave")
        await _settle(pilot)
        refused = _switches(runner)
        rows[("delegation", "status")] = _status_doc("off")  # the user installed it
        await _press(pilot, app, "dsave")
        await _settle(pilot)
        line = _text(app, "#claude")
    assert refused == []
    assert _switches(runner) == [("delegation", "on", "--json")]
    assert tui_design().words["claude_ready"] in line


# DS5 ------------------------------------------------------------------------

_SIGN_IN = "Claude Code is not signed in. Run claude and sign in, then run onex delegation on again."
_SETUP_FIX = (
    "Sign in to Claude Code yourself, then repeat onex setup --action repair. "
    "Existing login settings were preserved."
)
_REFUSED = (
    "Delegation is disabled, but its hook removal was refused. Preserve any "
    "edited delegation hooks and review onex delegation status before retrying off."
)


def _failed(status: str, fix: str, hooks: dict[str, object]) -> str:
    """A failed `onex delegation on|off --json` as cli_delegation._report prints it."""
    return json.dumps(
        {
            "status": status,
            "claude_code": "signed_out",
            "fix": fix,
            "observations": [
                {
                    "step": "claude-plugin",
                    "status": "action_required",
                    "fix": _SETUP_FIX,
                    "evidence": {"claude_code": "signed_out"},
                },
                {"step": "claude-hooks", **hooks},
            ],
        }
    )


@pytest.mark.parametrize(
    ("switch_on", "document", "expected"),
    [
        (
            True,
            _failed(
                "not_ready",
                _SIGN_IN,
                {"status": "unavailable", "fix": "Run onex setup"},
            ),
            _SIGN_IN,
        ),
        (
            False,
            _failed(
                "action_required",
                _SIGN_IN,
                {"status": "action_required", "fix": _REFUSED},
            ),
            _REFUSED,
        ),
    ],
    ids=["on-signed-out", "off-removal-refused"],
)
async def test_a_failed_save_quotes_the_delegation_commands_own_fix(
    switch_on: bool, document: str, expected: str
) -> None:
    """DS5."""
    rows = _selected_rows(connection=False)
    command = ("delegation", "on" if switch_on else "off")
    rows[command] = document
    runner = _CodedRunner(rows, {command: 1})
    before = "off" if switch_on else "hook"

    async def status() -> ModelTuiStatus:
        return ModelTuiStatus(models=2, delegation=before, hooks=_OFF)

    app = OnexTui(root=_root, runner=runner, load_status=status)
    async with app.run_test(size=(160, 46)) as pilot:
        await _settle(pilot, 4)
        await _open_tab(pilot)
        app.screen.set_all(switch_on)
        await _press(pilot, app, "dsave")
        await _settle(pilot)
        note = _text(app, "#dnote")
    assert expected in note, note
    assert "onex setup" not in note
