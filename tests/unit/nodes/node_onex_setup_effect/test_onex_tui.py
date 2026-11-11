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
from pathlib import Path

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
        self,
        lines: list[str],
        code: int = 0,
        captured: str = "",
        errors: str = "",
        by_command: dict[tuple[str, ...], str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.stdins: list[str | None] = []
        self._by_command = by_command or {}
        self.terminal_calls: list[tuple[str, ...]] = []
        self._lines = lines
        self._code = code
        self._captured = captured
        self._errors = errors

    async def capture(self, argv: tuple[str, ...]) -> tuple[int, str, str]:
        self.calls.append(argv)
        return self._code, self._by_command.get(argv[:2], self._captured), self._errors

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        self.calls.append(argv)
        self.stdins.append(stdin)
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
        for key in "/models connect":
            await pilot.press("space" if key == " " else "slash" if key == "/" else key)
        await pilot.press("enter")
        await pilot.pause()
        assert runner.terminal_calls == [("models", "connect")]
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


def test_a_folder_under_home_is_shown_with_a_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import display_path

    monkeypatch.setenv("HOME", str(tmp_path))
    assert display_path(tmp_path / "ws") == "~/ws"
    assert display_path(Path("/opt/elsewhere")) == "/opt/elsewhere"


async def _type(pilot: object, text: str) -> None:
    names = {" ": "space", "/": "slash", ":": "colon", ".": "full_stop", "-": "minus"}
    for key in text:
        await pilot.press(names.get(key, key))  # type: ignore[attr-defined]


async def test_shortcuts_lead_the_palette_like_coding_agent_tuis() -> None:
    app = _app(_Runner([]))
    async with app.run_test() as pilot:
        await pilot.press("slash")
        shown = [str(option.prompt) for option in app.query_one("#palette").options]
        assert [text.split()[0] for text in shown[:4]] == [
            "/setup",
            "/config",
            "/model",
            "/delegation",
        ]
        assert any(text.startswith("/models connections") for text in shown)


async def _press(pilot: object, app: OnexTui, button_id: str) -> None:
    app.screen.query_one(f"#{button_id}").press()
    await pilot.pause()  # type: ignore[attr-defined]


async def _open_config(pilot: object, app: OnexTui, command: str = "/config") -> None:
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import SetupScreen

    await _type(pilot, command)
    await pilot.press("enter")  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]
    assert isinstance(app.screen, SetupScreen)


async def test_config_finds_server_models_and_saves_the_ticked_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server provider: Find lists models; ticked ones are set up in this folder."""
    monkeypatch.chdir(tmp_path)
    found = json.dumps({"status": "ready", "models": ["llama3.2", "qwen3:8b"]})
    runner = _Runner([], by_command={("models", "discover"): found})
    app = _app(runner)
    async with app.run_test(size=(100, 44)) as pilot:
        await _open_config(pilot, app, "/setup")
        assert not app.screen.query_one("#key").display
        await _press(pilot, app, "find")
        await pilot.pause()
        assert ("models", "discover", "--url", "http://localhost:11434", "--json") in (
            runner.calls
        )
        listing = app.screen.query_one("#found")
        assert listing.option_count == 2
        listing.select("llama3.2")
        listing.select("qwen3:8b")
        await _press(pilot, app, "save")
        await pilot.pause()
        await pilot.pause()
        assert runner.calls[-2] == (
            "setup",
            "--no-interactive",
            "--yes",
            "--workspace",
            str(tmp_path),
            "--provider",
            "ollama",
            "--model",
            "llama3.2",
            "--url",
            "http://localhost:11434",
        )
        assert runner.calls[-1] == (
            "models",
            "add-model",
            "ollama",
            "--model",
            "qwen3:8b",
        )
        assert "ollama: llama3.2, qwen3:8b set up." in _log_text(app)


async def test_config_takes_several_providers_and_keeps_keys_out_of_argv_and_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hosted provider: key first, then its model; a second provider is added too."""
    monkeypatch.chdir(tmp_path)
    runner = _Runner(["qwen/qwen3.8-27b"])
    app = _app(runner)
    async with app.run_test(size=(100, 44)) as pilot:
        await _open_config(pilot, app)
        screen = app.screen
        screen.query_one("#provider").value = "openrouter"
        await pilot.pause()
        assert screen.query_one("#key").display
        assert not screen.query_one("#url").display
        screen.query_one("#key").value = "sk-or-secret-value"
        await _press(pilot, app, "find")
        await pilot.pause()
        assert runner.calls[-1] == ("models", "default-model", "openrouter")
        assert runner.stdins[-1] == "sk-or-secret-value\n"
        screen.query_one("#found").select("qwen/qwen3.8-27b")
        await _press(pilot, app, "add")
        await pilot.pause()
        screen.query_one("#provider").value = "ollama"
        await pilot.pause()
        screen.query_one("#model").value = "llama3.2"
        await _press(pilot, app, "add")
        await pilot.pause()
        basket = str(screen.query_one("#basket").render())
        assert "openrouter: qwen/qwen3.8-27b" in basket
        assert "ollama: llama3.2" in basket
        await _press(pilot, app, "save")
        await pilot.pause()
        await pilot.pause()
        setups = [call for call in runner.calls if call[0] == "setup"]
        assert [call[call.index("--provider") + 1] for call in setups] == [
            "openrouter",
            "ollama",
        ]
        assert all("sk-or-secret-value" not in " ".join(call) for call in runner.calls)
        assert "sk-or-secret-value" not in _log_text(app)


async def test_config_refuses_to_save_with_no_model_selected() -> None:
    runner = _Runner([])
    app = _app(runner)
    async with app.run_test(size=(100, 44)) as pilot:
        await _open_config(pilot, app)
        await _press(pilot, app, "save")
        await pilot.pause()
        assert "at least one model" in str(app.screen.query_one("#note").render())
        assert not [call for call in runner.calls if call[0] == "setup"]


async def test_delegation_turns_claude_code_off_and_on() -> None:
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import DelegationScreen

    runner = _Runner([])
    app = _app(runner)
    async with app.run_test(size=(100, 44)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DelegationScreen)
        assert app.screen.query_one("#target").value == "claude"
        await _press(pilot, app, "dsave")
        await pilot.pause()
        await pilot.pause()
        assert runner.calls[-1] == ("claude", "disable", "--yes")
        assert "Claude Code delegation is off." in _log_text(app)

        await _type(pilot, "/delegation")
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#on").value = True
        await pilot.pause()
        await _press(pilot, app, "dsave")
        await pilot.pause()
        assert "fill the gateway URL" in str(screen.query_one("#dnote").render())
        screen.query_one("#gateway-url").value = "http://127.0.0.1:8085"
        screen.query_one("#gateway-model").value = "Qwen3.8-27B"
        screen.query_one("#credential-ref").value = "onex/gateway"
        await _press(pilot, app, "dsave")
        await pilot.pause()
        await pilot.pause()
        assert runner.calls[-1] == (
            "claude",
            "enable",
            "--gateway-url",
            "http://127.0.0.1:8085",
            "--model",
            "Qwen3.8-27B",
            "--credential-ref",
            "onex/gateway",
            "--yes",
        )


def test_welcome_banner_names_omninode_delegation_and_the_main_commands() -> None:
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import welcome_banner

    text = welcome_banner().plain
    assert "D E L E G A T I O N" in text
    for command in ("/setup", "/config", "/model", "/delegation"):
        assert command in text


async def test_model_lists_set_up_models_and_makes_the_choice_first() -> None:
    rows = "\n".join(
        json.dumps(row)
        for row in (
            {"provider": "gemini", "set_up": False, "models": []},
            {
                "provider": "ollama",
                "set_up": True,
                "models": ["llama3.2", "qwen3:8b"],
                "tried_first": True,
                "first_model": "llama3.2",
            },
        )
    )
    runner = _Runner([], by_command={("models", "list"): rows})
    app = _app(runner)
    async with app.run_test(size=(100, 40)) as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        await pilot.pause()
        options = app.screen.query_one("#models")
        shown = [str(options.get_option_at_index(i).prompt) for i in range(2)]
        assert shown == ["llama3.2  ·  ollama  ✓ current", "qwen3:8b  ·  ollama"]
        await pilot.press("down", "enter")
        await pilot.pause()
        await pilot.pause()
        assert runner.calls[-1] == ("models", "first", "ollama", "qwen3:8b")


async def test_model_with_nothing_set_up_points_to_setup() -> None:
    app = _app(_Runner([], by_command={("models", "list"): ""}))
    async with app.run_test() as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        await pilot.pause()
        assert "Type /setup to connect one." in _log_text(app)


async def test_clear_empties_the_output() -> None:
    app = _app(_Runner([]))
    async with app.run_test() as pilot:
        await _type(pilot, "/clear")
        await pilot.press("enter")
        await pilot.pause()
        assert _log_text(app).strip() == ""


def test_config_offers_ollama_openrouter_gemini_and_openai_with_their_hints() -> None:
    from omnimarket.nodes.node_onex_setup_effect import cli_tui

    labels = [label for label, _ in cli_tui._PROVIDERS]
    assert labels == sorted(labels)
    assert [value for _, value in cli_tui._PROVIDERS] == [
        "gemini",
        "ollama",
        "openai",
        "openrouter",
    ]
    assert set(cli_tui._PROVIDER_HINTS) == {"ollama", "openrouter", "gemini", "openai"}
    assert "openrouter.ai/keys" in cli_tui._PROVIDER_HINTS["openrouter"]
    assert "aistudio.google.com/apikey" in cli_tui._PROVIDER_HINTS["gemini"]
