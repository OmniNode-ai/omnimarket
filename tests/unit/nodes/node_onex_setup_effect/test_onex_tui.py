# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The onex terminal UI (OMN-23413 Amendments 2 and 3), driven headless.

Failure modes:
  F1. The `/` palette lists a hand-kept subset, so a command a package adds
      through an entry point never appears (AC17).
  F2. A palette command runs something other than the CLI command it names.
  F3. Free text is not delegated, or its answer, model, cost and run id are lost
      (AC18).
  F4. A command or delegation that fails crashes the app or hides the reason.
  F5. A command that prompts (consent, hidden key, interactive setup) is run
      inside the output pane, where it can never be answered.
  F6. Esc does not close the palette, or quitting needs more than one key.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import click
import pytest

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    command_tree,
)

pytestmark = pytest.mark.unit


@click.group()
def _root() -> None:
    """Fixture root."""


@_root.group("models")
def _models() -> None:
    """Model servers."""


@_models.command("connections")
def _connections() -> None:
    """List named model connections."""


@_models.command("connect")
@click.option("--yes", is_flag=True)
def _connect(yes: bool) -> None:
    """Connect a model server."""


@_root.command("setup")
@click.option("--interactive/--no-interactive", default=True)
def _setup(interactive: bool) -> None:
    """Set up ONEX on this machine."""


def test_command_tree_lists_every_leaf_with_help_and_terminal_need() -> None:
    """F1, F5."""
    tree = {entry.path: entry for entry in command_tree(_root)}
    assert set(tree) == {("models", "connections"), ("models", "connect"), ("setup",)}
    assert tree[("models", "connections")].help == "List named model connections."
    assert not tree[("models", "connections")].needs_terminal
    assert tree[("models", "connect")].needs_terminal
    assert tree[("setup",)].needs_terminal


def test_command_tree_picks_up_a_command_added_later() -> None:
    """F1: nothing is hard-coded; a newly registered command appears."""

    @_models.command("benchmark")
    def _benchmark() -> None:
        """Time each model."""

    try:
        assert ("models", "benchmark") in {e.path for e in command_tree(_root)}
    finally:
        _models.commands.pop("benchmark")


def test_command_tree_of_the_real_onex_has_the_setup_models_and_claude_commands() -> (
    None
):
    from omnibase_core.cli.cli_commands import cli

    paths = {entry.path for entry in command_tree(cli)}
    for expected in (
        ("setup",),
        ("delegate",),
        ("models", "connect"),
        ("claude", "enable"),
        ("claude", "status"),
        ("tui",),
    ):
        assert expected in paths, expected


class _Runner:
    def __init__(
        self, lines: list[str], code: int = 0, captured: str = "", errors: str = ""
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.terminal_calls: list[tuple[str, ...]] = []
        self._lines = lines
        self._code = code
        self._captured = captured
        self._errors = errors

    async def capture(self, argv: tuple[str, ...]) -> tuple[int, str, str]:
        self.calls.append(argv)
        return self._code, self._captured, self._errors

    async def stream(self, argv: tuple[str, ...], emit: Callable[[str], None]) -> int:
        self.calls.append(argv)
        for line in self._lines:
            emit(line)
        return self._code

    def in_terminal(self, argv: tuple[str, ...]) -> int:
        self.terminal_calls.append(argv)
        return 0


def _app(runner: _Runner) -> OnexTui:
    async def status() -> ModelTuiStatus:
        return ModelTuiStatus(workspace="~/ws", model="Qwen3.8-27B", gateway=False)

    return OnexTui(root=_root, runner=runner, load_status=status)


def _log_text(app: OnexTui) -> str:
    return "\n".join(str(line) for line in app.query_one("#output").lines)


async def test_slash_opens_a_filtered_palette_and_escape_closes_it() -> None:
    """F1, F6."""
    app = _app(_Runner([]))
    async with app.run_test() as pilot:
        await pilot.press("slash", "c", "o", "n", "n")
        palette = app.query_one("#palette")
        assert palette.display
        shown = [str(option.prompt) for option in palette.options]
        assert any("models connections" in text for text in shown)
        assert any("models connect " in text for text in shown)
        assert not any(text.startswith("/setup") for text in shown)
        await pilot.press("escape")
        assert not palette.display


async def test_enter_runs_the_named_cli_command_and_shows_its_output() -> None:
    """F2."""
    runner = _Runner(["openrouter  ready  qwen/qwen3.8-27b"])
    app = _app(runner)
    async with app.run_test() as pilot:
        for key in "/models connections":
            await pilot.press("space" if key == " " else "slash" if key == "/" else key)
        await pilot.press("enter")
        await pilot.pause()
        assert runner.calls == [("models", "connections")]
        assert "qwen/qwen3.8-27b" in _log_text(app)


_DELEGATED = json.dumps(
    {
        "status": "success",
        "run_id": "175dde92-4151-4d52-ac68-aca5e4042afd",
        "result": {
            "status": "completed",
            "model_name": "Qwen3.8-27B",
            "response": "A receipt proves the run happened.",
            "metrics": {
                "total_tokens": 2257,
                "latency_ms": 6724,
                "cost_usd": 0.0,
                "cost_savings_usd": 0.010386,
            },
        },
    }
)


async def test_plain_text_is_delegated_and_its_receipt_reaches_the_status_bar() -> None:
    """F3: the answer, model, tokens, time, cost, saving and run id are shown."""
    runner = _Runner([], captured="diagnostic line\n" + _DELEGATED + "\n")
    app = _app(runner)
    async with app.run_test() as pilot:
        for key in "hello":
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause()
        assert runner.calls == [("delegate", "hello", "--json")]
        text = _log_text(app)
        assert "A receipt proves the run happened." in text
        assert "Qwen3.8-27B" in text
        assert "2,257 tokens" in text
        assert "6.7 s" in text
        assert "saved $0.0104" in text
        status = str(app.query_one("#status").render())
        assert "$0.0000" in status
        assert "175dde92" in status


async def test_a_failed_command_shows_its_exit_and_the_app_keeps_running() -> None:
    """F4."""
    failed = json.dumps(
        {
            "status": "failed",
            "result": {
                "terminal_payload": {
                    "status": "failed",
                    "terminal_failure_reason": "quality_gate_refused",
                }
            },
        }
    )
    runner = _Runner([], code=1, captured=failed, errors="onex delegate failed")
    app = _app(runner)
    async with app.run_test() as pilot:
        for key in "hi":
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause()
        text = _log_text(app)
        assert "quality_gate_refused" in text
        assert "exit 1" in text
        assert app.is_running


async def test_a_prompting_command_runs_in_the_real_terminal() -> None:
    """F5."""
    runner = _Runner([])
    app = _app(runner)
    async with app.run_test() as pilot:
        for key in "/setup":
            await pilot.press("slash" if key == "/" else key)
        await pilot.press("enter")
        await pilot.pause()
        assert runner.terminal_calls == [("setup",)]
        assert runner.calls == []


async def test_status_bar_shows_workspace_model_and_gateway() -> None:
    app = _app(_Runner([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        status = str(app.query_one("#status").render())
        assert "~/ws" in status
        assert "Qwen3.8-27B" in status
        assert "gateway off" in status


async def test_slash_quit_exits() -> None:
    """F6."""
    app = _app(_Runner([]))
    async with app.run_test() as pilot:
        for key in "/quit":
            await pilot.press("slash" if key == "/" else key)
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_code == 0


async def test_up_and_down_recall_earlier_entries_when_the_palette_is_closed() -> None:
    """Earlier tasks and commands come back with the arrow keys, newest first."""
    app = _app(_Runner([], captured=_DELEGATED))
    async with app.run_test() as pilot:
        for entry in ("first", "second"):
            for key in entry:
                await pilot.press(key)
            await pilot.press("enter")
            await pilot.pause()
        prompt = app.query_one("#prompt")
        await pilot.press("up")
        assert prompt.value == "second"
        await pilot.press("up")
        assert prompt.value == "first"
        await pilot.press("down")
        assert prompt.value == "second"
        await pilot.press("down")
        assert prompt.value == ""
