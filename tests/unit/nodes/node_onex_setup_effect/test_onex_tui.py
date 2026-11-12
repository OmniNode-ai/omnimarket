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
  F7. /config asks for a typed model id, or shows a list without context,
      price, capabilities and status, or loses ticks the user made (AC22).
  F8. /delegation asks for a URL, model or credential, or turns Claude Code
      delegation on with no selected model (AC25, AC26).
  F9. /model shows a different list or different words than /config, or an
      unknown price reads as free (AC20, AC27).
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

#: models.dev as the fixture states it: no test reaches the network.
_MODELS_DEV = {
    ("openrouter", "qwen/qwen3.8-27b"): {
        "name": "Qwen3.8 27B",
        "tool_call": True,
        "reasoning": True,
        "attachment": False,
        "modalities": {"input": ["text", "image"]},
        "limit": {"context": 262144, "output": 32768},
        "cost": {"input": 0.2, "output": 0.8, "cache_read": 0.02},
    },
}


def _index():
    from omnimarket.handlers.model_catalog import ModelsDevIndex

    return ModelsDevIndex(
        models=_MODELS_DEV,
        fetched_at="2026-11-12T00:00:00+00:00",
        stale=False,
        error=None,
    )


@pytest.fixture(autouse=True)
def _no_network_index(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnimarket.nodes.node_onex_setup_effect import cli_tui

    monkeypatch.setattr(cli_tui, "_default_index", _index)
    monkeypatch.setattr(cli_tui, "_default_read_server", lambda _base, _models: {})


def _found(app: OnexTui):
    from omnimarket.nodes.node_onex_setup_effect.cli_model_table import ModelTable

    return app.screen.query_one("#found", ModelTable)


def _tick(app: OnexTui, *model_ids: str) -> None:
    """Tick rows by id, in the order given (the order delegation tries them)."""
    from omnimarket.nodes.node_onex_setup_effect.cli_model_table import entry_key

    table = _found(app)
    by_id = {entry.model_id: entry for entry in table.entries}
    table.set_ticked(
        [
            *(entry_key(e) for e in table.ticked),
            *(entry_key(by_id[m]) for m in model_ids),
        ]
    )


async def _wait_for_models[T](pilot: Pilot[T]) -> None:
    """The list loads on its own once the key or URL stops changing."""
    from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
        tui_design,
    )

    await pilot.pause(tui_design().auto_find_seconds + 0.4)
    await pilot.pause()


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
                "evidence": {"local": "llama3.2; backend=local-url; tested_at=now"},
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
    """Server provider: the list loads on its own; ticked ones are set up here."""
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
    async with app.run_test(size=(120, 44)) as pilot:
        await _open_config(pilot, app)
        assert not app.screen.query_one("#key").display
        assert not app.screen.query("#model")
        assert not app.screen.query("#find")
        await _wait_for_models(pilot)
        assert ("models", "discover", "--url", _OLLAMA_DEFAULT, "--json") in (
            runner.calls
        )
        assert [e.model_id for e in _found(app).entries] == ["llama3.2", "qwen3:8b"]
        _tick(app, "llama3.2", "qwen3:8b")
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
            "local",
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
        assert "✓ models  local: llama3.2" in text
        assert "✓ also added qwen3:8b" in text
        assert "Ready: Local model · llama3.2, qwen3:8b" in text
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
    """Hosted provider: key, then its models with full details; a second provider too."""
    monkeypatch.chdir(tmp_path)
    list_models, seen = _hosted_listing()
    found = json.dumps({"status": "ready", "models": ["llama3.2"]})
    runner = _Runner([], by_command={("models", "discover"): found})
    app = OnexTui(
        root=_root, runner=runner, load_status=_status, list_models=list_models
    )
    async with app.run_test(size=(140, 50)) as pilot:
        await _open_config(pilot, app)
        screen = app.screen
        screen.query_one("#provider").value = "openrouter"
        await pilot.pause()
        assert screen.query_one("#key").display
        assert not screen.query_one("#url").display
        await _wait_for_models(pilot)
        assert seen == []  # no key, nothing sent
        screen.query_one("#key").value = "sk-or-secret-value"
        await _wait_for_models(pilot)
        assert seen == [("openrouter", "sk-or-secret-value")]
        rows = {e.model_id: e for e in _found(app).entries}
        qwen = rows["qwen/qwen3.8-27b"]
        # The provider's own context and price win; models.dev fills the rest.
        assert qwen.context_tokens == 131072
        assert str(qwen.input_usd_per_million) == "0.15"
        assert qwen.sources["price"] == "OpenRouter"
        assert qwen.capabilities["Tools"] == "yes"
        assert qwen.capabilities["Vision"] == "yes"
        assert qwen.max_output_tokens == 32768
        free = rows["openrouter/free"]
        assert free.output_usd_per_million is None  # not stated: never shown as free
        _tick(app, "qwen/qwen3.8-27b")
        await _press(pilot, app, "add")
        screen.query_one("#provider").value = "local"
        await _wait_for_models(pilot)
        _tick(app, "llama3.2")
        await _press(pilot, app, "add")
        basket = str(screen.query_one("#basket").render())
        assert "openrouter: qwen/qwen3.8-27b" in basket
        assert "local: llama3.2" in basket
        await _press(pilot, app, "save")
        await pilot.pause()
        setups = [call for call in runner.calls if call[0] == "setup"]
        assert [call[call.index("--provider") + 1] for call in setups] == [
            "openrouter",
            "local",
        ]
        assert runner.stdins[runner.calls.index(setups[0])] == "sk-or-secret-value\n"
        assert all("sk-or-secret-value" not in " ".join(call) for call in runner.calls)
        assert "sk-or-secret-value" not in _log_text(app)


async def test_a_long_list_is_searchable_and_ticks_survive_searching() -> None:
    """F7: search narrows the rows; a tick made before it is kept, in tick order."""
    from textual.widgets import DataTable

    list_models, _ = _hosted_listing(count=30)
    app = OnexTui(
        root=_root, runner=_Runner([]), load_status=_status, list_models=list_models
    )
    async with app.run_test(size=(140, 50)) as pilot:
        await _open_config(pilot, app)
        screen = app.screen
        screen.query_one("#provider").value = "openrouter"
        await pilot.pause()
        screen.query_one("#key").value = "sk-or-long-list"
        await _wait_for_models(pilot)
        table = _found(app)
        rows = table.query_one(DataTable)
        assert rows.row_count == 30
        _tick(app, "vendor/model-07")
        table.query_one("#mt-search").value = "qwen"
        await pilot.pause()
        assert rows.row_count == 1
        rows.focus()
        await pilot.press("space")  # the keyboard tick on the one shown row
        await pilot.pause()
        table.query_one("#mt-search").value = ""
        await pilot.pause()
        assert [e.model_id for e in table.ticked] == [
            "vendor/model-07",
            "qwen/qwen3.8-27b",
        ]
        await _press(pilot, app, "add")
        assert "openrouter: vendor/model-07, qwen/qwen3.8-27b" in str(
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
        screen.query_one("#key").value = "sk-bad-key-value"
        await _wait_for_models(pilot)
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
        screen.query_one("#key").value = "sk-or-switching"
        await _wait_for_models(pilot)
        _tick(app, "openrouter/free")
        screen.query_one("#provider").value = "gemini"
        await pilot.pause()
        assert not screen.query_one("#found").display
        assert _found(app).ticked == ()
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


def _selected_rows(*, gateway: bool) -> dict[tuple[str, ...], str]:
    """`models list` and `models connections` as the CLI prints them."""
    rows = "\n".join(
        json.dumps(row)
        for row in (
            {
                "provider": "openrouter",
                "set_up": True,
                "models": ["qwen/qwen3.8-27b"],
                "last_test": {"model": "qwen/qwen3.8-27b", "status": "passed"},
            },
            {"provider": "ollama", "set_up": True, "models": ["llama3.2"]},
        )
    )
    connection = {
        "connection_id": "lab-gateway",
        "provider": "openai_compatible",
        "url": "http://127.0.0.1:8085",
        "models": ["Qwen3.8-27B"],
        "native_messages": True,
    }
    listing = {"status": "ready", "connections": [connection] if gateway else []}
    return {("models", "list"): rows, ("models", "connections"): json.dumps(listing)}


async def test_delegation_asks_for_nothing_and_shows_the_selected_models() -> None:
    """F8: no URL, model or credential field; the table is the stored selection."""
    from textual.widgets import Input

    from omnimarket.nodes.node_onex_setup_effect.cli_tui import DelegationScreen

    runner = _Runner([], by_command=_selected_rows(gateway=False))
    app = _app(runner)
    async with app.run_test(size=(140, 44)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DelegationScreen)
        typed = [i.id for i in screen.query(Input) if i.id != "mt-search"]
        assert typed == []
        table = screen.query_one("#selected")
        shown = [(e.provider, e.model_id, e.status) for e in table.entries]
        assert shown == [
            ("openrouter", "qwen/qwen3.8-27b", "ready"),
            ("ollama", "llama3.2", "untested"),
        ]
        assert table.entries[0].capabilities["Tools"] == "yes"  # same rows as /config


async def test_delegation_hook_mode_runs_the_design_commands_on_and_off() -> None:
    from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
        tui_design,
    )

    hook = next(m for m in tui_design().delegation.modes if m.id == "hook")
    runner = _Runner([], by_command=_selected_rows(gateway=False))
    app = _app(runner)
    async with app.run_test(size=(140, 44)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        app.screen.query_one("#on").value = True
        await _press(pilot, app, "dsave")
        await pilot.pause()
        await pilot.pause()
        assert runner.calls[-len(hook.enable) :] == list(hook.enable)
        assert "Claude Code delegation is on: Subagent tasks" in _log_text(app)
        assert "Welcome To OmniNode" not in _log_text(app)  # one line only

        await _type(pilot, "/delegation")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        app.screen.query_one("#on").value = False
        await _press(pilot, app, "dsave")
        await pilot.pause()
        await pilot.pause()
        assert runner.calls[-len(hook.disable) :] == list(hook.disable)
        assert "Claude Code delegation is off." in _log_text(app)


async def test_delegation_refuses_to_turn_on_with_no_selected_model() -> None:
    """F8: without a model there is nothing to delegate to, so nothing runs."""
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import DelegationScreen
    from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
        tui_design,
    )

    empty = {("models", "list"): "", ("models", "connections"): ""}
    runner = _Runner([], by_command=empty)
    app = _app(runner)
    async with app.run_test(size=(140, 44)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        before = len(runner.calls)
        app.screen.query_one("#on").value = True
        await _press(pilot, app, "dsave")
        await pilot.pause()
        assert isinstance(app.screen, DelegationScreen)
        assert tui_design().delegation.no_models in str(
            app.screen.query_one("#dnote").render()
        )
        assert len(runner.calls) == before


async def test_gateway_mode_fills_connection_and_model_from_the_selection() -> None:
    """The gateway gets a selected model whose connection speaks native Messages."""
    runner = _Runner([], by_command=_selected_rows(gateway=True))
    app = _app(runner)
    async with app.run_test(size=(140, 44)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        app.screen.query_one("#mode").value = "gateway"
        app.screen.query_one("#on").value = True
        await pilot.pause()
        await _press(pilot, app, "dsave")
        await pilot.pause()
        await pilot.pause()
        assert runner.calls[-1][-4:] == (
            "--gateway-connection",
            "lab-gateway",
            "--gateway-model",
            "Qwen3.8-27B",
        )


async def test_gateway_mode_refuses_with_no_gateway_capable_model() -> None:
    from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
        tui_design,
    )

    runner = _Runner([], by_command=_selected_rows(gateway=False))
    app = _app(runner)
    async with app.run_test(size=(140, 44)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        before = len(runner.calls)
        app.screen.query_one("#mode").value = "gateway"
        app.screen.query_one("#on").value = True
        await pilot.pause()
        await _press(pilot, app, "dsave")
        await pilot.pause()
        assert tui_design().delegation.no_gateway_model in str(
            app.screen.query_one("#dnote").render()
        )
        assert len(runner.calls) == before


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
    """F9: /model shows the same table; Enter makes a model first, A goes back to Auto."""
    from textual.widgets import DataTable

    rows = "\n".join(
        json.dumps(row)
        for row in (
            {"provider": "gemini", "set_up": False, "models": []},
            {
                "provider": "local",
                "set_up": True,
                "models": ["llama3.2", "qwen3:8b"],
                "tried_first": True,
                "first_model": "llama3.2",
            },
        )
    )
    runner = _Runner([], by_command={("models", "list"): rows})
    app = _app(runner)
    async with app.run_test(size=(140, 40)) as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        table = app.screen.query_one("#models")
        assert [e.model_id for e in table.entries] == ["llama3.2", "qwen3:8b"]
        assert "Now: llama3.2 (Local model)" in str(
            app.screen.query_one("#pnow").render()
        )
        assert table.query_one(DataTable).has_focus
        await pilot.press("down", "enter")
        await pilot.pause()
        await pilot.pause()
        assert runner.calls[-1] == ("models", "first", "local", "qwen3:8b")
        assert "qwen3:8b is now tried first (Local model)" in _log_text(app)

        await _type(pilot, "/model")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.pause()
        assert runner.calls[-1] == ("models", "first", "--clear")


async def test_model_with_nothing_set_up_points_to_setup() -> None:
    app = _app(_Runner([], by_command={("models", "list"): ""}))
    async with app.run_test() as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        await pilot.pause()
        assert "Type /config to connect one." in _log_text(app)


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
    # One entry for every OpenAI-compatible server: no separate Ollama or vLLM.
    assert [entry.provider for entry in choices] == [
        "gemini",
        "local",
        "openai",
        "openrouter",
    ]
    assert [e.provider for e in choices if not cli_tui._hosted(e.provider)] == ["local"]
    assert cli_tui._default_url("local").endswith(":11434")
    assert all(entry.hint for entry in choices)
    for entry in choices:
        assert cli_tui._hosted(entry.provider) is not traits.needs_url(entry.provider)
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
        assert not screen.query("#find")
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
        assert len(_found(app).entries) == 2
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
        screen.query_one("#key").value = "sk-or-every-model"
        await _wait_for_models(pilot)
        assert len(_found(app).entries) == 466
        _tick(app, *(e.model_id for e in _found(app).entries))
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
    found = json.dumps({"status": "ready", "models": ["llama3.2"]})
    runner = _Runner(
        [],
        code=1,
        by_command={
            ("setup", "--no-interactive"): failed,
            ("models", "discover"): found,
        },
    )
    app = _app(runner)
    async with app.run_test(size=(120, 50)) as pilot:
        await _open_config(pilot, app)
        await _wait_for_models(pilot)
        _tick(app, "llama3.2")
        await _press(pilot, app, "save")
        await pilot.pause()
        text = _log_text(app)
        assert "✗ models: start Ollama, then run /config again" in text
        assert "Ready:" not in text


async def test_setup_is_not_a_tui_command_config_is() -> None:
    """/config collects the choices and runs onex setup, so /setup is not a second
    way in: it is neither listed nor run, and says how to find the commands."""
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import SetupScreen

    runner = _Runner([])
    app = _app(runner)
    async with app.run_test(size=(120, 50)) as pilot:
        await _type(pilot, "/setup ")
        await pilot.press("backspace", "enter")
        await pilot.pause()
        assert not isinstance(app.screen, SetupScreen)
        assert "no onex command matches '/setup'" in _log_text(app)
        assert not [call for call in runner.calls if call[0] == "setup"]


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
    from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
        tui_design,
    )

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
        screen.query_one("#key").value = "sk-or-long-list"
        await _wait_for_models(pilot)
        for button in ("add", "save", "cancel"):
            region = screen.query_one(f"#{button}").region
            assert region.height > 0, button
            assert region.bottom <= 30, button
        assert (
            str(screen.query_one("#keys").render())
            == (tui_design().model_table.help["config"])
        )
        _tick(app, "qwen/qwen3.8-27b")
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
        await _wait_for_models(pilot)
        _tick(app, "big-27b", "small-1b")
        await pilot.pause()
        await _press(pilot, app, "save")
        await pilot.pause()
        await pilot.pause()
        text = _log_text(app)
        assert "adding small-1b: Error: small-1b did not answer" in text
        assert "Ready: Local model · big-27b." in text
        assert "Ready: Local model · big-27b, small-1b" not in text


async def test_switching_server_provider_moves_an_unedited_default_but_keeps_a_typed_url() -> (
    None
):
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import _default_url

    app = _app(_Runner([]))
    async with app.run_test(size=(120, 44)) as pilot:
        await _open_config(pilot, app)
        screen = app.screen
        assert screen.query_one("#url").value == _default_url("local")
        screen.query_one("#url").value = "http://gpu-box:8000"
        screen.query_one("#provider").value = "openrouter"
        await pilot.pause()
        screen.query_one("#provider").value = "local"
        await pilot.pause()
        assert screen.query_one("#url").value == "http://gpu-box:8000"


async def test_model_names_each_local_model_by_the_server_that_runs_it() -> None:
    """An Ollama model added by URL is shown as Ollama, with what Ollama said;
    a value no source states shows the unknown mark, never '?' or a made-up one."""
    from rich.text import Text
    from textual.widgets import DataTable

    from omnimarket.models.model_local_server_details import ModelLocalServerDetails
    from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
        tui_design,
    )

    rows = json.dumps(
        {
            "provider": "local",
            "set_up": True,
            "models": ["Qwen3.8-27B", "saluki:latest"],
            "endpoints": {
                "Qwen3.8-27B": "http://vllm.test:8000",
                "saluki:latest": "http://ollama.test:11434",
            },
        }
    )
    asked: list[tuple[str, tuple[str, ...]]] = []

    def read_server(base: str, models: tuple[str, ...]):
        asked.append((base, models))
        if "ollama" in base:
            return {
                "saluki:latest": ModelLocalServerDetails(
                    server="Ollama",
                    context_tokens=40960,
                    capabilities={"Tools": "yes"},
                )
            }
        return {"Qwen3.8-27B": ModelLocalServerDetails(server="vLLM")}

    app = OnexTui(
        root=_root,
        runner=_Runner([], by_command={("models", "list"): rows}),
        load_status=_status,
        read_server=read_server,
    )
    async with app.run_test(size=(160, 40)) as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert sorted(asked) == [
            ("http://ollama.test:11434", ("saluki:latest",)),
            ("http://vllm.test:8000", ("Qwen3.8-27B",)),
        ]
        grid = app.screen.query_one("#models").query_one(DataTable)
        cells = {
            str(grid.get_row_at(i)[1]): [str(c) for c in grid.get_row_at(i)]
            for i in range(grid.row_count)
        }
        mark = tui_design().model_table.unknown_mark
        assert cells["saluki:latest"][0] == "Local model · Ollama"
        assert cells["saluki:latest"][2] == "41K"
        columns = [c.key for c in tui_design().model_table.columns if c.key != "pick"]
        caps = columns.index("capabilities")
        assert "Tools" in cells["saluki:latest"][caps]
        assert cells["Qwen3.8-27B"][0] == "Local model · vLLM"
        assert cells["Qwen3.8-27B"][2] == mark
        assert cells["Qwen3.8-27B"][caps] == mark
        assert not any("?" in c for row in cells.values() for c in row)
        assert isinstance(grid.get_row_at(0)[0], Text)


# The six regressions the tabs, one-click ticks and gateway changes guard against:
#   T1: Tab and Shift+Tab do not cycle between /config, /model, /delegation tabs,
#       or clicking a tab does not open it.
#   T2: Clicking a model row in /config does not tick it, or the list does not
#       receive keyboard focus on load.
#   T3: Saving /config with no models ticked fails to show a warning, or runs an
#       action despite the warning.
#   T4: /delegation prints the full setup output instead of a single result
#       line, or omits the failing step name and its fix.
#   T5: The local model server is queried more than once, or a 404/405 response
#       to the Anthropic Messages route is not treated as "no".
#   T6: Gateway mode fails to register the server as a connection, or starts the
#       gateway only after user input instead of automatically.


async def test_tab_and_shift_tab_cycle_the_three_screens_and_a_click_opens_one() -> (
    None
):
    """T1."""
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
        DelegationScreen,
        ModelPickScreen,
        SetupScreen,
    )

    app = _app(_Runner([], by_command=_selected_rows(gateway=False)))
    async with app.run_test(size=(140, 44)) as pilot:
        await _open_config(pilot, app)
        seen = [type(app.screen)]
        for _ in range(3):
            await pilot.press("tab")
            await pilot.pause()
            await pilot.pause()
            seen.append(type(app.screen))
        assert seen == [SetupScreen, ModelPickScreen, DelegationScreen, SetupScreen]
        await pilot.press("shift+tab")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, DelegationScreen)
        await pilot.click("#tab-model")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, ModelPickScreen)


async def test_one_click_ticks_a_row_and_the_list_takes_focus_when_it_loads() -> None:
    """T2."""
    from textual.widgets import DataTable

    list_models, _ = _hosted_listing()
    app = OnexTui(
        root=_root, runner=_Runner([]), load_status=_status, list_models=list_models
    )
    async with app.run_test(size=(140, 50)) as pilot:
        await _open_config(pilot, app)
        app.screen.query_one("#provider").value = "openrouter"
        await pilot.pause()
        app.screen.query_one("#key").value = "sk-or-one-click"
        await _wait_for_models(pilot)
        rows = _found(app).query_one(DataTable)
        assert rows.has_focus
        await pilot.click(rows, offset=(30, 2))  # the second data row, once
        await pilot.pause()
        assert len(_found(app).ticked) == 1


async def test_saving_with_nothing_ticked_warns_and_runs_nothing() -> None:
    """T3."""
    from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
        tui_design,
    )

    runner = _Runner([])
    app = _app(runner)
    async with app.run_test(size=(120, 44)) as pilot:
        await _open_config(pilot, app)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert tui_design().delegation.nothing_ticked in str(
            app.screen.query_one("#note").render()
        )
        assert not [call for call in runner.calls if call[0] == "setup"]


async def test_a_failed_switch_prints_one_line_with_the_step_and_its_fix() -> None:
    """T4: setup's banner and step list never reach the pane; the stopping step does."""
    stopped = json.dumps(
        {
            "observations": [
                {"step": "preflight", "status": "success"},
                {
                    "step": "claude-plugin",
                    "status": "action_required",
                    "fix": "Install or repair the customer asset bundle. The rest.",
                },
            ]
        }
    )
    rows = _selected_rows(gateway=False)
    runner = _Runner(
        ["Welcome To OmniNode Delegation Onboarding"],
        code=1,
        by_command={**rows, ("setup", "--yes"): stopped},
    )
    app = _app(runner)
    async with app.run_test(size=(140, 44)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        app.screen.query_one("#on").value = True
        await _press(pilot, app, "dsave")
        await pilot.pause()
        await pilot.pause()
        text = _log_text(app)
        # The one line wraps in the pane: read it as plain text, wrap undone.
        flat = " ".join(
            " ".join(line.text for line in app.query_one("#output").lines).split()
        )
        assert "not switched on: claude-plugin" in flat
        assert "Install or repair the customer asset bundle" in flat
        assert "Welcome To OmniNode" not in text
        assert "The rest" not in text


async def test_gateway_registers_a_server_that_speaks_messages_then_starts_it() -> None:
    """T6: nothing typed: the connection comes from the server the model runs on."""
    from omnimarket.models.model_local_server_details import ModelLocalServerDetails
    from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
        tui_design,
    )

    rows = json.dumps(
        {
            "provider": "local",
            "set_up": True,
            "models": ["saluki:latest"],
            "endpoints": {"saluki:latest": "http://ollama.test:11434"},
        }
    )
    runner = _Runner(
        [],
        by_command={
            ("models", "list"): rows,
            ("models", "connections"): json.dumps({"connections": []}),
        },
    )

    def read_server(base: str, models: tuple[str, ...]):
        return {
            m: ModelLocalServerDetails(server="Ollama", native_messages=True)
            for m in models
        }

    app = OnexTui(
        root=_root, runner=runner, load_status=_status, read_server=read_server
    )
    async with app.run_test(size=(140, 44)) as pilot:
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        app.screen.query_one("#mode").value = "gateway"
        app.screen.query_one("#on").value = True
        await pilot.pause()
        await _press(pilot, app, "dsave")
        await pilot.pause()
        await pilot.pause()
        connect = [c for c in runner.calls if c[:2] == ("models", "connect")]
        assert connect == [
            tuple(
                part.format(
                    connection="ollama-11434",
                    url="http://ollama.test:11434",
                    model="saluki:latest",
                )
                for part in tui_design().delegation.connect
            )
        ]
        setup = [c for c in runner.calls if c[0] == "setup"]
        assert setup[-1][-4:] == (
            "--gateway-connection",
            "ollama-11434",
            "--gateway-model",
            "saluki:latest",
        )
        assert runner.calls.index(connect[0]) < runner.calls.index(setup[-1])
