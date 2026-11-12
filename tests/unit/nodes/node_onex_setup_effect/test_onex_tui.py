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
from textual.pilot import Pilot

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    command_tree,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.setup_contract_values import (
    loopback_url,
)

pytestmark = pytest.mark.unit

_OLLAMA_DEFAULT = loopback_url(11434)


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

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        self.calls.append(argv)
        self.stdins.append(stdin)
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


async def _status() -> ModelTuiStatus:
    return ModelTuiStatus(workspace="~/ws", model="Qwen3.8-27B", gateway=False)


def _app(runner: _Runner) -> OnexTui:
    return OnexTui(root=_root, runner=runner, load_status=_status)


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


_SETUP_REPORT = json.dumps(
    {
        "observations": [
            {
                "step": "preflight",
                "status": "success",
                "changed": False,
                "evidence": {},
            },
            {
                "step": "workspace",
                "status": "success",
                "changed": True,
                "evidence": {"workspace": "/tmp/ws"},
            },
            {
                "step": "models",
                "status": "success",
                "changed": True,
                "evidence": {"ollama": "llama3.2; backend=local-url; tested_at=now"},
            },
        ]
    },
    indent=2,
)


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


async def _type[T](pilot: Pilot[T], text: str) -> None:
    names = {" ": "space", "/": "slash", ":": "colon", ".": "full_stop", "-": "minus"}
    for key in text:
        await pilot.press(names.get(key, key))


async def test_shortcuts_lead_the_palette_like_coding_agent_tuis() -> None:
    app = _app(_Runner([]))
    async with app.run_test() as pilot:
        await pilot.press("slash")
        shown = [str(option.prompt) for option in app.query_one("#palette").options]
        assert [text.split()[0] for text in shown[:4]] == [
            "/config",
            "/model",
            "/delegation",
            "/clear",
        ]
        assert not any(text.split()[0] == "/setup" for text in shown)
        assert any(text.startswith("/models connections") for text in shown)


async def _press[T](pilot: Pilot[T], app: OnexTui, button_id: str) -> None:
    app.screen.query_one(f"#{button_id}").press()
    await pilot.pause()


async def _open_config[T](
    pilot: Pilot[T], app: OnexTui, command: str = "/config"
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import SetupScreen

    await _type(pilot, command)
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, SetupScreen)


async def test_config_finds_server_models_and_saves_the_ticked_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server provider: Find lists models; ticked ones are set up in this folder."""
    monkeypatch.chdir(tmp_path)
    found = json.dumps({"status": "ready", "models": ["llama3.2", "qwen3:8b"]})
    runner = _Runner(
        [],
        by_command={
            ("models", "discover"): found,
            ("setup", "--no-interactive"): _SETUP_REPORT,
        },
    )
    app = _app(runner)
    async with app.run_test(size=(100, 44)) as pilot:
        await _open_config(pilot, app, "/setup")
        assert not app.screen.query_one("#key").display
        await _press(pilot, app, "find")
        await pilot.pause()
        assert ("models", "discover", "--url", _OLLAMA_DEFAULT, "--json") in (
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
            _OLLAMA_DEFAULT,
            "--json",
        )
        assert runner.calls[-1] == (
            "models",
            "add",
            "local",
            "--url",
            _OLLAMA_DEFAULT,
            "--model",
            "llama3.2",
            "--model",
            "qwen3:8b",
        )
        text = _log_text(app)
        assert "✓ workspace" in text
        assert "✓ models  ollama: llama3.2" in text
        assert "✓ also added qwen3:8b" in text
        assert "Ready: Ollama · llama3.2, qwen3:8b" in text
        assert "Welcome To OmniNode Delegation Onboarding" not in text


def _hosted_listing(
    count: int = 2, outcome: str = "listed", message: str | None = None
):
    from omnimarket.handlers.routing.byok_model_listing import (
        ModelByokListedModel,
        ModelByokModelListing,
    )

    seen: list[tuple[str, str]] = []
    models = [
        ModelByokListedModel(
            id="qwen/qwen3.8-27b",
            name="Qwen: Qwen3.8 27B",
            context_tokens=131072,
            input_usd_per_million=0.15,
            output_usd_per_million=0.6,
        ),
        ModelByokListedModel(
            id="openrouter/free", name="Free", input_usd_per_million=0.0
        ),
    ] + [
        ModelByokListedModel(id=f"vendor/model-{i:02d}", name=f"M{i}")
        for i in range(count - 2)
    ]

    def list_models(provider: str, key: str) -> ModelByokModelListing:
        seen.append((provider, key))
        return ModelByokModelListing(
            provider=provider,
            outcome=outcome,
            models=tuple(models) if outcome == "listed" else (),
            message=message,
        )

    return list_models, seen


async def test_config_takes_several_providers_and_keeps_keys_out_of_argv_and_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hosted provider: key first, then its models with details; a second provider too."""
    monkeypatch.chdir(tmp_path)
    list_models, seen = _hosted_listing()
    runner = _Runner([])
    app = OnexTui(
        root=_root, runner=runner, load_status=_status, list_models=list_models
    )
    async with app.run_test(size=(120, 50)) as pilot:
        await _open_config(pilot, app)
        screen = app.screen
        screen.query_one("#provider").value = "openrouter"
        await pilot.pause()
        assert screen.query_one("#key").display
        assert not screen.query_one("#url").display
        await _press(pilot, app, "find")
        assert "enter your OpenRouter API key first" in str(
            screen.query_one("#note").render()
        )
        assert seen == []
        screen.query_one("#key").value = "sk-or-secret-value"
        await _press(pilot, app, "find")
        await pilot.pause()
        assert seen == [("openrouter", "sk-or-secret-value")]
        listing = screen.query_one("#found")
        rows = [str(listing.get_option_at_index(i).prompt) for i in range(2)]
        assert rows[1] == "openrouter/free   Free   free"
        assert rows[0] == (
            "qwen/qwen3.8-27b   Qwen: Qwen3.8 27B   131K ctx   $0.15 in / $0.60 out per 1M"
        )
        listing.select("qwen/qwen3.8-27b")
        await pilot.pause()
        await _press(pilot, app, "add")
        screen.query_one("#provider").value = "ollama"
        await pilot.pause()
        screen.query_one("#model").value = "llama3.2"
        await _press(pilot, app, "add")
        basket = str(screen.query_one("#basket").render())
        assert "openrouter: qwen/qwen3.8-27b" in basket
        assert "ollama: llama3.2" in basket
        await _press(pilot, app, "save")
        await pilot.pause()
        setups = [call for call in runner.calls if call[0] == "setup"]
        assert [call[call.index("--provider") + 1] for call in setups] == [
            "openrouter",
            "ollama",
        ]
        assert runner.stdins[runner.calls.index(setups[0])] == "sk-or-secret-value\n"
        assert all("sk-or-secret-value" not in " ".join(call) for call in runner.calls)
        assert "sk-or-secret-value" not in _log_text(app)


async def test_a_long_list_gets_a_filter_and_ticks_survive_filtering() -> None:
    list_models, _ = _hosted_listing(count=30)
    app = OnexTui(
        root=_root, runner=_Runner([]), load_status=_status, list_models=list_models
    )
    async with app.run_test(size=(120, 50)) as pilot:
        await _open_config(pilot, app)
        screen = app.screen
        screen.query_one("#provider").value = "openrouter"
        await pilot.pause()
        screen.query_one("#key").value = "k"
        await _press(pilot, app, "find")
        await pilot.pause()
        listing = screen.query_one("#found")
        assert screen.query_one("#filter").display
        assert listing.option_count == 30
        listing.select("vendor/model-07")
        await pilot.pause()
        screen.query_one("#filter").value = "qwen"
        await pilot.pause()
        assert listing.option_count == 1
        listing.select("qwen/qwen3.8-27b")
        await pilot.pause()
        screen.query_one("#filter").value = ""
        await pilot.pause()
        assert set(listing.selected) == {"vendor/model-07", "qwen/qwen3.8-27b"}
        await _press(pilot, app, "add")
        assert "openrouter: qwen/qwen3.8-27b, vendor/model-07" in str(
            screen.query_one("#basket").render()
        )


async def test_a_refused_key_shows_the_provider_message_and_lists_nothing() -> None:
    list_models, _ = _hosted_listing(outcome="rejected", message="Invalid API key")
    app = OnexTui(
        root=_root, runner=_Runner([]), load_status=_status, list_models=list_models
    )
    async with app.run_test(size=(120, 50)) as pilot:
        await _open_config(pilot, app)
        screen = app.screen
        screen.query_one("#provider").value = "openai"
        await pilot.pause()
        screen.query_one("#key").value = "bad"
        await _press(pilot, app, "find")
        await pilot.pause()
        assert "Invalid API key" in str(screen.query_one("#note").render())
        assert not screen.query_one("#found").display


async def test_switching_provider_clears_the_previous_list_and_ticks() -> None:
    list_models, _ = _hosted_listing()
    app = OnexTui(
        root=_root, runner=_Runner([]), load_status=_status, list_models=list_models
    )
    async with app.run_test(size=(120, 50)) as pilot:
        await _open_config(pilot, app)
        screen = app.screen
        screen.query_one("#provider").value = "openrouter"
        await pilot.pause()
        screen.query_one("#key").value = "k"
        await _press(pilot, app, "find")
        await pilot.pause()
        screen.query_one("#found").select("openrouter/free")
        screen.query_one("#provider").value = "gemini"
        await pilot.pause()
        assert not screen.query_one("#found").display
        await _press(pilot, app, "save")
        assert "at least one model" in str(screen.query_one("#note").render())


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
    from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
        tui_design,
    )

    design = tui_design()
    for word in ("WELCOME TO", "OMNINODE", "DELEGATION"):
        first_row = "  ".join(design.font[c][0] for c in word)
        assert first_row in text, word  # every banner word drawn big
    assert "█" in text
    for command in ("/config", "/model", "/delegation"):
        assert command in text
    assert "/setup" not in text


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
        assert "qwen3:8b is now tried first (Ollama)" in _log_text(app)


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


def test_config_providers_come_from_the_traits_file_sorted_by_label() -> None:
    """Nothing hard-coded: the choices, labels, hints and ports are data."""
    from omnimarket.nodes.node_onex_setup_effect import cli_tui
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
        setup_provider_traits,
    )

    traits = setup_provider_traits()
    choices = cli_tui._picker()
    labels = [entry.label for entry in choices]
    assert labels == sorted(labels, key=str.lower)
    assert [entry.provider for entry in choices] == [
        "gemini",
        "ollama",
        "openai",
        "openrouter",
        "local",
    ]
    assert cli_tui._default_url("local").endswith(":8000")
    assert all(entry.hint for entry in choices)
    for entry in choices:
        assert cli_tui._hosted(entry.provider) is not traits.needs_url(entry.provider)
    assert cli_tui._default_url("ollama").endswith(":11434")
    assert cli_tui._default_url("openrouter") == ""
    assert "openrouter.ai/keys" in cli_tui._hint("openrouter")


async def test_entering_a_key_loads_the_models_without_pressing_find() -> None:
    """The list follows the key: no button, one read, only the latest key."""
    list_models, seen = _hosted_listing()
    app = OnexTui(
        root=_root, runner=_Runner([]), load_status=_status, list_models=list_models
    )
    async with app.run_test(size=(120, 50)) as pilot:
        await _open_config(pilot, app)
        screen = app.screen
        screen.query_one("#provider").value = "openrouter"
        await pilot.pause()
        screen.query_one("#key").value = "sk-or"
        await pilot.pause(1.2)
        assert seen == []  # too short to be a key: nothing is sent
        screen.query_one("#key").value = "sk-or-first-attempt"
        await pilot.pause(0.2)
        screen.query_one("#key").value = "sk-or-final-key-value"
        await pilot.pause(1.5)
        assert seen == [("openrouter", "sk-or-final-key-value")]
        assert screen.query_one("#found").option_count == 2
        assert "2 found" in str(screen.query_one("#note").render())


async def test_enter_on_a_partial_command_runs_the_highlighted_entry() -> None:
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import SetupScreen

    app = _app(_Runner([]))
    async with app.run_test(size=(120, 50)) as pilot:
        await _type(pilot, "/conf")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        assert "no onex command matches" not in _log_text(app)


async def test_every_model_is_listed_with_no_cap_and_any_number_can_be_ticked() -> None:
    list_models, _ = _hosted_listing(count=466)
    app = OnexTui(
        root=_root, runner=_Runner([]), load_status=_status, list_models=list_models
    )
    async with app.run_test(size=(120, 50)) as pilot:
        await _open_config(pilot, app)
        screen = app.screen
        screen.query_one("#provider").value = "openrouter"
        await pilot.pause()
        screen.query_one("#key").value = "k"
        await _press(pilot, app, "find")
        await pilot.pause()
        listing = screen.query_one("#found")
        assert listing.option_count == 466
        listing.select_all()
        await pilot.pause()
        await _press(pilot, app, "add")
        basket = str(screen.query_one("#basket").render())
        assert basket.count(",") == 465


async def test_a_failed_setup_step_shows_its_fix_and_no_ready_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    failed = json.dumps(
        {
            "observations": [
                {"step": "preflight", "status": "success", "changed": False},
                {
                    "step": "models",
                    "status": "failed",
                    "changed": False,
                    "evidence": {},
                    "fix": "start Ollama, then run /config again",
                },
            ]
        }
    )
    runner = _Runner([], code=1, by_command={("setup", "--no-interactive"): failed})
    app = _app(runner)
    async with app.run_test(size=(120, 50)) as pilot:
        await _open_config(pilot, app)
        app.screen.query_one("#model").value = "llama3.2"
        await _press(pilot, app, "save")
        await pilot.pause()
        text = _log_text(app)
        assert "✗ models: start Ollama, then run /config again" in text
        assert "Ready:" not in text


async def test_typing_setup_still_opens_the_config_form() -> None:
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import SetupScreen

    app = _app(_Runner([]))
    async with app.run_test(size=(120, 50)) as pilot:
        await _type(pilot, "/setup ")
        await pilot.press("backspace", "enter")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)


async def test_the_banner_sits_in_the_middle_of_a_wide_screen() -> None:
    app = _app(_Runner([]))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        lines = [str(line.text) for line in app.query_one("#output").lines]
        tagline = next(line for line in lines if "costs least" in line)
        left = len(tagline) - len(tagline.lstrip())
        assert left > 20  # centred, not pinned to the left edge


async def test_centred_banner_keeps_letters_and_tips_aligned() -> None:
    """Each section is centred as a block: a word's three rows, and the tips,
    start in the same column."""
    app = _app(_Runner([]))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        lines = [str(line.text) for line in app.query_one("#output").lines]

        def start(line: str) -> int:
            return len(line) - len(line.lstrip())

        from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
            tui_design,
        )

        design = tui_design()
        for word in design.banner.words:
            rows = ["  ".join(design.font[c][r] for c in word.text) for r in range(3)]
            columns = {
                next(line.index(row) for line in lines if row in line) for row in rows
            }
            assert len(columns) == 1, word.text  # all three rows share one column
        keys = [tip.key for tip in design.banner.tips]
        tips = [line for line in lines if any(f" {key}" in line for key in keys[1:3])]
        assert len({start(t) for t in tips}) == 1
        assert start(tips[0]) > 20


async def test_save_stays_on_screen_with_a_long_list_and_ctrl_s_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    list_models, _ = _hosted_listing(count=466)
    runner = _Runner([], by_command={("setup", "--no-interactive"): _SETUP_REPORT})
    app = OnexTui(
        root=_root, runner=runner, load_status=_status, list_models=list_models
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_config(pilot, app)
        screen = app.screen
        screen.query_one("#provider").value = "openrouter"
        await pilot.pause()
        screen.query_one("#key").value = "k"
        await _press(pilot, app, "find")
        await pilot.pause()
        for button in ("find", "add", "save", "cancel"):
            region = screen.query_one(f"#{button}").region
            assert region.height > 0, button
            assert region.bottom <= 30, button
        assert "Ctrl+S saves" in str(screen.query_one("#keys").render())
        screen.query_one("#found").select("qwen/qwen3.8-27b")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.pause()
        assert any(call[0] == "setup" for call in runner.calls)


async def test_models_that_fail_to_add_are_not_called_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ready names only what was set up; a failed extra shows its error."""
    monkeypatch.chdir(tmp_path)
    found = json.dumps({"status": "ready", "models": ["big-27b", "small-1b"]})

    class _AddFails(_Runner):
        async def capture(
            self, argv: tuple[str, ...], stdin: str | None = None
        ) -> tuple[int, str, str]:
            if argv[:2] == ("models", "add"):
                self.calls.append(argv)
                return 1, "", "Error: small-1b did not answer"
            return await super().capture(argv, stdin)

    runner = _AddFails(
        [],
        by_command={
            ("models", "discover"): found,
            ("setup", "--no-interactive"): _SETUP_REPORT,
        },
    )
    app = _app(runner)
    async with app.run_test(size=(100, 44)) as pilot:
        await _open_config(pilot, app)
        await _press(pilot, app, "find")
        app.screen.query_one("#found").select("big-27b")
        app.screen.query_one("#found").select("small-1b")
        await pilot.pause()
        await _press(pilot, app, "save")
        await pilot.pause()
        await pilot.pause()
        text = _log_text(app)
        assert "adding small-1b: Error: small-1b did not answer" in text
        assert "Ready: Ollama · big-27b." in text
        assert "Ready: Ollama · big-27b, small-1b" not in text


async def test_switching_server_provider_moves_an_unedited_default_but_keeps_a_typed_url() -> (
    None
):
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import _default_url

    app = _app(_Runner([]))
    async with app.run_test(size=(120, 44)) as pilot:
        await _open_config(pilot, app)
        screen = app.screen
        assert screen.query_one("#url").value == _default_url("ollama")
        screen.query_one("#provider").value = "local"
        await pilot.pause()
        assert screen.query_one("#url").value == _default_url("local")
        screen.query_one("#url").value = "http://gpu-box:8000"
        screen.query_one("#provider").value = "ollama"
        await pilot.pause()
        assert screen.query_one("#url").value == "http://gpu-box:8000"
