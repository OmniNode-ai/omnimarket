# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex tui`: a full-screen front end over the onex CLI (OMN-23413).

Laid out like open-source coding-agent terminal UIs: an output pane, one input
line, a status bar. `/` opens a palette built from the CLI's own command tree,
so a command any package registers appears with no change here. Text typed
without `/` is sent to `onex delegate`. Every action runs the same `onex`
command the CLI runs; nothing here re-implements setup, routing or settings.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

import click
from pydantic import BaseModel, ConfigDict, SecretStr
from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    OptionList,
    RichLog,
    Select,
    SelectionList,
    Static,
    Switch,
)
from textual.widgets.option_list import Option

from omnimarket.handlers.routing.byok_model_listing import (
    ModelByokListedModel,
    ModelByokModelListing,
    list_byok_models,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.setup_contract_values import (
    loopback_url,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
    ModelSetupPickerProvider,
    setup_provider_traits,
)

_PROMPTING_OPTIONS = frozenset(("interactive", "yes"))
# Shortcuts first, as coding-agent TUIs list them; every CLI command follows.
_LOCAL_COMMANDS = {
    ("setup",): "Connect a model server and choose its model.",
    ("config",): "Change the model server, model or key.",
    ("model",): "Pick the model every task tries first.",
    ("delegation",): "Turn delegation on or off for Claude Code.",
    ("clear",): "Clear the output.",
    ("help",): "Show keys and how commands work.",
    ("quit",): "Leave the TUI.",
}


def _picker() -> tuple[ModelSetupPickerProvider, ...]:
    """What /config offers, read from setup_provider_traits.yaml at call time."""
    return setup_provider_traits().picker_providers()


def _entry(provider: str) -> ModelSetupPickerProvider | None:
    return next((e for e in _picker() if e.provider == provider), None)


def _label(provider: str) -> str:
    entry = _entry(provider)
    return entry.label if entry else provider


def _hint(provider: str) -> str:
    entry = _entry(provider)
    return entry.hint if entry else ""


def _hosted(provider: str) -> bool:
    return not setup_provider_traits().needs_url(provider)


def _default_url(provider: str) -> str:
    entry = _entry(provider)
    return loopback_url(entry.default_port) if entry and entry.default_port else ""


class ModelTuiCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: tuple[str, ...]
    help: str
    needs_terminal: bool

    @property
    def label(self) -> str:
        return "/" + " ".join(self.path)


class ModelTuiStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: str
    model: str | None = None
    gateway: bool = False


class ModelTuiSetupChoice(BaseModel):
    """One provider and the models chosen for it; the key never reaches argv or the log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    url: str | None
    models: tuple[str, ...]
    key: SecretStr | None = None

    def argv(self, workspace: Path) -> tuple[str, ...]:
        argv: tuple[str, ...] = (
            "setup",
            "--no-interactive",
            "--yes",
            "--workspace",
            str(workspace),
        )
        argv += ("--provider", self.provider, "--model", self.models[0])
        return argv + (("--url", self.url) if self.url else ())

    def more_models_argv(self) -> tuple[str, ...] | None:
        """`models add-model` for every model after the first, in the order chosen."""
        if len(self.models) < 2:
            return None
        extra = tuple(part for m in self.models[1:] for part in ("--model", m))
        return ("models", "add-model", self.provider, *extra)


class ProtocolTuiRunner(Protocol):
    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int: ...

    async def capture(self, argv: tuple[str, ...]) -> tuple[int, str, str]: ...

    def in_terminal(self, argv: tuple[str, ...]) -> int: ...


def _needs_terminal(command: click.Command) -> bool:
    """A command that can prompt runs in the real terminal, not the output pane."""
    for param in command.params:
        if isinstance(param, click.Option) and (
            param.prompt or param.hide_input or param.name in _PROMPTING_OPTIONS
        ):
            return True
    return False


def command_tree(root: click.Group) -> tuple[ModelTuiCommand, ...]:
    """Every leaf command under ``root``, read from click at call time."""
    found: list[ModelTuiCommand] = []

    def walk(group: click.Group, ctx: click.Context, prefix: tuple[str, ...]) -> None:
        for name in group.list_commands(ctx):
            try:
                command = group.get_command(ctx, name)
            except Exception:  # an extension that fails to load is left out, not fatal
                continue
            if command is None or command.hidden:
                continue
            path = (*prefix, name)
            if isinstance(command, click.Group):
                walk(command, click.Context(command, parent=ctx, info_name=name), path)
                if not command.invoke_without_command:
                    continue
            found.append(
                ModelTuiCommand(
                    path=path,
                    help=command.get_short_help_str(limit=72),
                    needs_terminal=_needs_terminal(command),
                )
            )

    walk(root, click.Context(root, info_name="onex"), ())
    return tuple(sorted(found, key=lambda entry: entry.path))


def display_path(path: Path) -> str:
    """A folder under the user's home shown as ~/..., as terminals show it."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


class HandlerOnexCommand:
    """Runs the installed `onex` beside this interpreter, as a user would."""

    def __init__(self) -> None:
        self._onex = str(Path(sys.executable).parent / "onex")

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        process = await asyncio.create_subprocess_exec(
            self._onex,
            *argv,
            stdin=asyncio.subprocess.DEVNULL
            if stdin is None
            else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None
        if stdin is not None and process.stdin is not None:
            process.stdin.write(stdin.encode())
            await process.stdin.drain()
            process.stdin.close()
        async for raw in process.stdout:
            emit(raw.decode(errors="replace").rstrip("\n"))
        return await process.wait()

    async def capture(self, argv: tuple[str, ...]) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            self._onex,
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        return (
            await process.wait(),
            out.decode(errors="replace"),
            err.decode(errors="replace"),
        )

    def in_terminal(self, argv: tuple[str, ...]) -> int:
        return subprocess.run([self._onex, *argv], check=False).returncode


def _last_json_object(text: str) -> dict[str, object] | None:
    """The delegate result: the last line of stdout that parses as an object."""
    for line in reversed(text.splitlines()):
        if line.lstrip().startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _failure_reason(document: dict[str, object]) -> str | None:
    result = document.get("result")
    if not isinstance(result, dict):
        return None
    for source in (result, result.get("terminal_payload")):
        if isinstance(source, dict):
            for key in ("terminal_failure_reason", "error_message"):
                if source.get(key):
                    return str(source[key])
    return None


async def load_status() -> ModelTuiStatus:
    """Default model and gateway state, read through the CLI's own JSON output."""
    runner = HandlerOnexCommand()
    model: str | None = None
    gateway = False
    models: list[str] = []
    await runner.stream(("models", "list", "--json"), models.append)
    for line in models:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        first = row.get("first_model") or next(iter(row.get("models") or []), None)
        if row.get("set_up") and first:
            model = model or str(first)
            if row.get("tried_first"):
                model = str(first)
                break
    claude: list[str] = []
    await runner.stream(("claude", "status", "--json"), claude.append)
    for line in claude:
        try:
            gateway = bool(json.loads(line).get("enabled"))
        except ValueError:
            continue
    return ModelTuiStatus(
        workspace=display_path(Path.cwd()), model=model, gateway=gateway
    )


def _discovered_models(text: str) -> tuple[str | None, list[str]]:
    """`models discover --json`: (message when nothing was found, model ids)."""
    document = _last_json_object(text) or {}
    models = document.get("models")
    found = [str(m) for m in models] if isinstance(models, list) else []
    message = document.get("message")
    return (None if found else str(message or "no models found")), found


def model_row_label(model: ModelByokListedModel) -> str:
    """One picker row: id, then whatever the provider published about the model."""
    parts = [model.id]
    if model.name != model.id:
        parts.append(model.name)
    if model.context_tokens:
        tokens = model.context_tokens
        parts.append(
            f"{tokens // 1_000_000}M ctx"
            if tokens >= 1_000_000
            else f"{tokens // 1000}K ctx"
        )
    if model.input_usd_per_million is not None:
        if model.input_usd_per_million == 0 and not model.output_usd_per_million:
            parts.append("free")
        else:
            out = model.output_usd_per_million
            parts.append(
                f"${model.input_usd_per_million:.2f} in"
                + (f" / ${out:.2f} out" if out is not None else "")
                + " per 1M"
            )
    return "   ".join(parts)


ListModels = Callable[[str, str], ModelByokModelListing]


class SetupScreen(ModalScreen[tuple[ModelTuiSetupChoice, ...] | None]):
    """Provider, then its key or URL, then any number of its models; repeat per provider."""

    CSS = """
    SetupScreen { align: center middle; }
    #form { width: 76; height: auto; max-height: 95%; border: round $accent; padding: 1 2; background: $panel; }
    #form Input, #form Select { margin-bottom: 1; }
    #found { height: auto; max-height: 8; display: none; margin-bottom: 1; }
    #note { color: $text-muted; }
    #basket { color: $success; margin-bottom: 1; }
    #buttons { height: auto; align-horizontal: right; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self, runner: ProtocolTuiRunner, list_models: ListModels | None = None
    ) -> None:
        super().__init__()
        self._runner = runner
        self._list_models: ListModels = list_models or list_byok_models
        self._basket: dict[str, ModelTuiSetupChoice] = {}
        self._rows: list[tuple[str, str]] = []
        self._ticked: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Label("Models for delegation: provider, key or URL, then models")
            choices = _picker()
            first = next((e for e in choices if e.selected), choices[0])
            yield Select(
                [(e.label, e.provider) for e in choices],
                value=first.provider,
                allow_blank=False,
                id="provider",
            )
            yield Input(placeholder="API key", password=True, id="key")
            yield Input(
                value=_default_url(first.provider), placeholder="Server URL", id="url"
            )
            yield Input(placeholder="Filter models", id="filter")
            yield SelectionList[str](id="found")
            yield Input(placeholder="Or type a model id", id="model")
            yield Static("Find models lists what this provider offers.", id="note")
            yield Static("Selected: none yet (at least one is needed)", id="basket")
            with Horizontal(id="buttons"):
                yield Button("Find models", id="find")
                yield Button("Add", id="add")
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#filter").display = False
        self._show_fields()
        self.query_one("#note", Static).update(_hint(self._provider()))

    def _provider(self) -> str:
        return str(self.query_one("#provider", Select).value)

    def _value(self, widget_id: str) -> str:
        return self.query_one(f"#{widget_id}", Input).value.strip()

    def _show_fields(self) -> None:
        hosted = _hosted(self._provider())
        url = self.query_one("#url", Input)
        if not hosted and not url.value.strip():
            url.value = _default_url(self._provider())
        self.query_one("#key").display = hosted
        self.query_one("#url").display = not hosted

    def on_select_changed(self, event: Select.Changed) -> None:
        self._show_fields()
        self.query_one("#note", Static).update(_hint(self._provider()))
        self._show_rows([])

    async def _find(self) -> None:
        note = self.query_one("#note", Static)
        provider = self._provider()
        if _hosted(provider):
            key = self._value("key")
            if not key:
                note.update(f"enter your {_label(provider)} API key first")
                return
            listing = await asyncio.to_thread(self._list_models, provider, key)
            rows = [(m.id, model_row_label(m)) for m in listing.models]
            message = listing.message
        else:
            _, out, err = await self._runner.capture(
                ("models", "discover", "--url", self._value("url"), "--json")
            )
            message, found = _discovered_models(out or err)
            rows = [(model, model) for model in found]
        self._show_rows(rows)
        note.update(message or f"{len(rows)} found: tick any number, then Add")

    def _show_rows(self, rows: list[tuple[str, str]]) -> None:
        """New results: forget earlier ticks, show the filter for a long list."""
        self._rows = rows
        self._ticked = set()
        self.query_one("#filter", Input).value = ""
        self.query_one("#filter").display = len(rows) > 10
        self._render_rows()

    def _render_rows(self) -> None:
        words = self._value("filter").lower().split()
        shown = [
            (label, model_id, model_id in self._ticked)
            for model_id, label in self._rows
            if all(word in label.lower() for word in words)
        ]
        listing = self.query_one("#found", SelectionList)
        with listing.prevent(SelectionList.SelectedChanged):
            listing.clear_options()
            listing.add_options(shown[:200])
        listing.display = bool(self._rows)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._render_rows()

    def on_selection_list_selected_changed(
        self, event: SelectionList.SelectedChanged[str]
    ) -> None:
        listing = event.selection_list
        visible = {
            str(listing.get_option_at_index(i).value)
            for i in range(listing.option_count)
        }
        self._ticked = (self._ticked - visible) | {str(v) for v in listing.selected}

    def _add_current(self) -> bool:
        provider = self._provider()
        chosen = [model_id for model_id, _ in self._rows if model_id in self._ticked]
        typed = self._value("model")
        if typed and typed not in chosen:
            chosen.append(typed)
        if not chosen:
            return False
        key = self._value("key")
        if _hosted(provider) and not key:
            self.query_one("#note", Static).update(
                f"{_label(provider)} needs an API key"
            )
            return False
        earlier = self._basket.get(provider)
        models = tuple(dict.fromkeys((*(earlier.models if earlier else ()), *chosen)))
        self._basket[provider] = ModelTuiSetupChoice(
            provider=provider,
            url=None if _hosted(provider) else self._value("url") or None,
            models=models,
            key=SecretStr(key) if key else None,
        )
        self._ticked = set()
        self._render_rows()
        self.query_one("#model", Input).value = ""
        self.query_one("#basket", Static).update(
            "Selected: "
            + "   ·   ".join(
                f"{c.provider}: {', '.join(c.models)}" for c in self._basket.values()
            )
        )
        return True

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        note = self.query_one("#note", Static)
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "find":
            note.update("finding models…")
            await self._find()
        elif event.button.id == "add":
            if self._add_current():
                note.update("added; pick another provider, or Save")
            else:
                note.update("tick or type a model first")
        elif event.button.id == "save":
            self._add_current()
            if not self._basket:
                note.update("select at least one model before saving")
                return
            self.dismiss(tuple(self._basket.values()))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ModelTuiDelegationChoice(BaseModel):
    """Claude Code delegation on (through an ONEX gateway) or off."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: str
    on: bool
    gateway_url: str = ""
    model: str = ""
    credential_ref: str = ""

    def argv(self) -> tuple[str, ...]:
        if not self.on:
            return ("claude", "disable", "--yes")
        return (
            "claude",
            "enable",
            "--gateway-url",
            self.gateway_url,
            "--model",
            self.model,
            "--credential-ref",
            self.credential_ref,
            "--yes",
        )


class DelegationScreen(ModalScreen[ModelTuiDelegationChoice | None]):
    """Pick a client, then switch its delegation on or off."""

    CSS = """
    DelegationScreen { align: center middle; }
    #deleg { width: 72; height: auto; border: round $accent; padding: 1 2; background: $panel; }
    #deleg Select, #deleg Input { margin-bottom: 1; }
    #switch-row { height: auto; margin-bottom: 1; }
    #switch-row Label { padding: 1 1 0 0; }
    #gateway { height: auto; }
    #dnote { color: $text-muted; margin-bottom: 1; }
    #dbuttons { height: auto; align-horizontal: right; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, enabled: bool) -> None:
        super().__init__()
        self._enabled = enabled

    def compose(self) -> ComposeResult:
        with Vertical(id="deleg"):
            yield Label("Delegation")
            yield Select(
                [("Claude Code", "claude")],
                value="claude",
                allow_blank=False,
                id="target",
            )
            with Horizontal(id="switch-row"):
                yield Label("Delegate Claude Code requests")
                yield Switch(value=self._enabled, id="on")
            with Vertical(id="gateway"):
                yield Input(placeholder="ONEX gateway URL", id="gateway-url")
                yield Input(placeholder="Model the gateway serves", id="gateway-model")
                yield Input(placeholder="Credential reference", id="credential-ref")
            yield Static(
                "On: new Claude Code sessions route through your ONEX gateway. "
                "Off: ONEX settings are removed; your own edits stay.",
                id="dnote",
            )
            with Horizontal(id="dbuttons"):
                yield Button("Save", variant="primary", id="dsave")
                yield Button("Cancel", id="dcancel")

    def on_mount(self) -> None:
        self.query_one("#gateway").display = self._enabled

    def on_switch_changed(self, event: Switch.Changed) -> None:
        self.query_one("#gateway").display = event.value

    def _value(self, widget_id: str) -> str:
        return self.query_one(f"#{widget_id}", Input).value.strip()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dcancel":
            self.dismiss(None)
            return
        on = self.query_one("#on", Switch).value
        fields = ("gateway-url", "gateway-model", "credential-ref")
        if on and not all(self._value(f) for f in fields):
            self.query_one("#dnote", Static).update(
                "To turn it on, fill the gateway URL, model and credential reference."
            )
            return
        self.dismiss(
            ModelTuiDelegationChoice(
                target=str(self.query_one("#target", Select).value),
                on=on,
                gateway_url=self._value("gateway-url"),
                model=self._value("gateway-model"),
                credential_ref=self._value("credential-ref"),
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class ModelPickScreen(ModalScreen[tuple[str, str] | None]):
    """Pick the model every task tries first."""

    CSS = """
    ModelPickScreen { align: center middle; }
    #pick { width: 72; height: auto; max-height: 20; border: round $accent; padding: 1 2; background: $panel; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, choices: list[tuple[str, str, bool]]) -> None:
        super().__init__()
        self._choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="pick"):
            yield Label("Choose the model every task tries first  (Esc to close)")
            yield OptionList(
                *(
                    Option(
                        f"{model}  ·  {provider}" + ("  ✓ current" if first else ""),
                        id=f"{provider}\t{model}",
                    )
                    for provider, model, first in self._choices
                ),
                id="models",
            )

    def on_mount(self) -> None:
        self.query_one("#models", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        provider, _, model = str(event.option.id).partition("\t")
        self.dismiss((provider, model))

    def action_cancel(self) -> None:
        self.dismiss(None)


def _model_choices(text: str) -> list[tuple[str, str, bool]]:
    """(provider, model, is first) for every set-up model in `models list --json`."""
    choices: list[tuple[str, str, bool]] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or not row.get("set_up"):
            continue
        for model in row.get("models") or []:
            first = bool(row.get("tried_first")) and model == row.get("first_model")
            choices.append((str(row["provider"]), str(model), first))
    return choices


_LETTERS = {
    "O": ("▄▀▀▄", "█  █", " ▀▀ "),
    "M": ("█▄ ▄█", "█ ▀ █", "▀   ▀"),
    "N": ("█▄  █", "█ ▀▄█", "▀   ▀"),
    "I": ("█", "█", "▀"),
    "D": ("█▀▀▄", "█  █", "▀▀▀ "),
    "E": ("█▀▀▀", "█▀▀ ", "▀▀▀▀"),
}
_GRADIENT = ("#00d2ff", "#3a9bff", "#6a7bff", "#9a5cf5", "#c64cec", "#ff4fa3")


def welcome_banner() -> Text:
    """OMNINODE in a cyan-to-pink gradient, DELEGATION under it, then the keys."""
    rows = ["  ".join(_LETTERS[c][r] for c in "OMNINODE") for r in range(3)]
    width = max(len(row) for row in rows)
    banner = Text("\n")
    for row in rows:
        banner.append("  ")
        for column, char in enumerate(row):
            banner.append(
                char, style=f"bold {_GRADIENT[column * len(_GRADIENT) // width]}"
            )
        banner.append("\n")
    banner.append(
        "  " + " ".join("DELEGATION").center(width) + "\n", style="bold #ffb347"
    )
    banner.append(
        "  Your tasks, your models: run them where it costs least.\n\n",
        style="italic #9aa5b1",
    )
    for key, text in (
        ("type a task", "and press Enter to delegate it"),
        ("/config", "providers, keys and models"),
        ("/setup", "connect a model server"),
        ("/model", "pick the model to use"),
        ("/delegation", "Claude Code on or off"),
        ("/", "every command   ·   ↑↓ history   ·   ctrl+c quit"),
    ):
        banner.append("  ▸ ", style="#6a7bff")
        banner.append(f"{key:<14}", style="bold #00d2ff")
        banner.append(f"{text}\n", style="#c9d1d9")
    return banner


class OnexTui(App[int]):
    """The onex terminal UI."""

    TITLE = "onex"
    CSS = """
    Screen { layout: vertical; }
    #output { height: 1fr; border: round $primary-background; padding: 0 1; }
    #palette { height: auto; max-height: 12; border: round $accent; display: none; }
    #prompt { border: round $accent; }
    #status { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    """
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("escape", "close_palette", "Close", show=False),
        Binding("tab", "complete", "Complete", show=False, priority=True),
        Binding("down", "palette_down", show=False, priority=True),
        Binding("up", "palette_up", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        root: click.Group | None = None,
        runner: ProtocolTuiRunner | None = None,
        load_status: Callable[[], Awaitable[ModelTuiStatus]] | None = None,
        list_models: ListModels | None = None,
    ) -> None:
        super().__init__()
        if root is None:
            from omnibase_core.cli.cli_commands import cli

            root = cli
        self._commands = command_tree(root)
        self._runner: ProtocolTuiRunner = runner or HandlerOnexCommand()
        self._list_models = list_models
        self._load_status = load_status or globals()["load_status"]
        self._status = ModelTuiStatus(workspace=display_path(Path.cwd()))
        self._last_run = ""
        self._history: list[str] = []
        self._recall = 0

    def compose(self) -> ComposeResult:
        yield RichLog(id="output", wrap=True, markup=False, highlight=False)
        yield OptionList(id="palette")
        yield Input(
            id="prompt",
            placeholder="Ask ONEX to delegate a task, or type / for commands",
        )
        yield Static(id="status")

    async def on_mount(self) -> None:
        self.query_one("#output", RichLog).write(welcome_banner())
        self._render_status()
        self.query_one("#prompt", Input).focus()
        self.run_worker(self._refresh_status(), exclusive=True, group="status")

    async def _refresh_status(self) -> None:
        try:
            loaded = await self._load_status()
            self._status = (
                loaded
                if loaded.model
                else loaded.model_copy(update={"model": self._status.model})
            )
        except Exception as error:  # status is a convenience; never crash on it
            self._write(f"status unavailable: {error}")
        self._render_status()

    def _render_status(self) -> None:
        parts = [
            self._status.workspace,
            f"model {self._status.model or 'not set'}",
            "gateway on" if self._status.gateway else "gateway off",
        ]
        if self._last_run:
            parts.append(self._last_run)
        parts.append("/ commands  ctrl+c quit")
        self.query_one("#status", Static).update("  ·  ".join(parts))

    def _write(self, text: str) -> None:
        self.query_one("#output", RichLog).write(text)

    def _matches(self, query: str) -> list[ModelTuiCommand]:
        words = query.lower().split()
        local = [
            ModelTuiCommand(path=path, help=text, needs_terminal=False)
            for path, text in _LOCAL_COMMANDS.items()
        ]
        return [
            entry
            for entry in (
                *local,
                *(c for c in self._commands if c.path not in _LOCAL_COMMANDS),
            )
            if all(word in " ".join(entry.path) for word in words)
        ]

    def on_input_changed(self, event: Input.Changed) -> None:
        palette = self.query_one("#palette", OptionList)
        if not event.value.startswith("/"):
            palette.display = False
            return
        palette.clear_options()
        palette.add_options(
            Option(f"{entry.label}  {entry.help}", id=" ".join(entry.path))
            for entry in self._matches(event.value[1:])[:50]
        )
        palette.display = palette.option_count > 0
        if palette.option_count:
            palette.highlighted = 0

    def action_close_palette(self) -> None:
        self.query_one("#palette", OptionList).display = False

    def _in_dialog(self, key: str) -> bool:
        """A form or picker is open: its own widgets get the arrow and Tab keys."""
        if len(self.screen_stack) < 2:
            return False
        focused = self.focused
        if key == "tab":
            self.screen.focus_next()
        elif isinstance(focused, OptionList):
            getattr(focused, f"action_cursor_{key}")()
        elif key in ("up", "down"):
            self.screen.focus_previous() if key == "up" else self.screen.focus_next()
        return True

    def action_palette_down(self) -> None:
        if self._in_dialog("down"):
            return
        palette = self.query_one("#palette", OptionList)
        if palette.display:
            palette.action_cursor_down()
        else:
            self._recall_entry(+1)

    def action_palette_up(self) -> None:
        if self._in_dialog("up"):
            return
        palette = self.query_one("#palette", OptionList)
        if palette.display:
            palette.action_cursor_up()
        else:
            self._recall_entry(-1)

    def _recall_entry(self, step: int) -> None:
        """Arrow keys walk earlier entries; past the newest is an empty line."""
        if not self._history:
            return
        self._recall = max(0, min(len(self._history), self._recall + step))
        prompt = self.query_one("#prompt", Input)
        prompt.value = (
            self._history[self._recall] if self._recall < len(self._history) else ""
        )
        prompt.cursor_position = len(prompt.value)

    def action_complete(self) -> None:
        if self._in_dialog("tab"):
            return
        palette = self.query_one("#palette", OptionList)
        if palette.display and palette.highlighted is not None:
            option = palette.get_option_at_index(palette.highlighted)
            prompt = self.query_one("#prompt", Input)
            prompt.value = f"/{option.id} "
            prompt.cursor_position = len(prompt.value)

    def _resolve(self, words: list[str]) -> ModelTuiCommand | None:
        known = {entry.path: entry for entry in self._commands}
        for size in range(len(words), 0, -1):
            entry = known.get(tuple(words[:size]))
            if entry is not None:
                return entry
        return None

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        prompt = self.query_one("#prompt", Input)
        prompt.value = ""
        self.action_close_palette()
        if not text:
            return
        self._history.append(text)
        self._recall = len(self._history)
        if not text.startswith("/"):
            await self._delegate(text)
            return
        try:
            words = shlex.split(text[1:])
        except ValueError as error:
            self._write(f"could not read that command: {error}")
            return
        if words in (["quit"], ["exit"]):
            self.exit(0)
            return
        if words == ["help"]:
            self._write(
                "Type a task and press Enter to delegate it. /setup or /config connects "
                "a model server, /model picks the model, /clear empties this pane. Type "
                "/ to list every onex command; keep typing to filter, Tab to complete, "
                "Enter to run, Esc to close. Up and Down recall earlier entries."
            )
            return
        if words == ["clear"]:
            self.query_one("#output", RichLog).clear()
            return
        if words in (["setup"], ["config"]):
            self.push_screen(
                SetupScreen(self._runner, self._list_models), self._apply_setup
            )
            return
        if words == ["model"]:
            await self._pick_model()
            return
        if words == ["delegation"]:
            self.push_screen(
                DelegationScreen(self._status.gateway), self._apply_delegation
            )
            return
        entry = self._resolve(words)
        if entry is None:
            self._write(
                f"no onex command matches '/{' '.join(words)}'; type / to list them"
            )
            return
        await self._run(entry, tuple(words))

    async def _run(self, entry: ModelTuiCommand, argv: tuple[str, ...]) -> None:
        self._write(f"$ onex {shlex.join(argv)}")
        if entry.needs_terminal:
            try:
                with self.suspend():
                    code = self._runner.in_terminal(argv)
            except SuspendNotSupported:  # headless or a terminal that cannot hand over
                code = self._runner.in_terminal(argv)
        else:
            code = await self._runner.stream(argv, self._write)
        if code:
            self._write(f"exit {code}")
        if entry.path[:1] in (("models",), ("claude",), ("setup",)):
            self.run_worker(self._refresh_status(), exclusive=True, group="status")

    async def _apply_setup(
        self, choices: tuple[ModelTuiSetupChoice, ...] | None
    ) -> None:
        for choice in choices or ():
            argv = choice.argv(Path.cwd())
            self._write(f"$ onex {shlex.join(argv)}")
            stdin = choice.key.get_secret_value() + "\n" if choice.key else None
            code = await self._runner.stream(argv, self._write, stdin=stdin)
            more = choice.more_models_argv()
            if not code and more:
                self._write(f"$ onex {shlex.join(more)}")
                code = await self._runner.stream(more, self._write)
            models = ", ".join(choice.models)
            self._write(
                f"exit {code}" if code else f"{choice.provider}: {models} set up."
            )
        if choices:
            self._write("Type a task to try it, or /model to choose which goes first.")
        self.run_worker(self._refresh_status(), exclusive=True, group="status")

    async def _apply_delegation(self, choice: ModelTuiDelegationChoice | None) -> None:
        if choice is None:
            return
        argv = choice.argv()
        self._write(f"$ onex {shlex.join(argv)}")
        code = await self._runner.stream(argv, self._write)
        state = "on" if choice.on else "off"
        self._write(f"exit {code}" if code else f"Claude Code delegation is {state}.")
        self.run_worker(self._refresh_status(), exclusive=True, group="status")

    async def _pick_model(self) -> None:
        _, out, _ = await self._runner.capture(("models", "list", "--json"))
        choices = _model_choices(out)
        if not choices:
            self._write("No model is set up yet. Type /setup to connect one.")
            return
        self.push_screen(ModelPickScreen(choices), self._apply_model)

    async def _apply_model(self, picked: tuple[str, str] | None) -> None:
        if picked is None:
            return
        argv = ("models", "first", *picked)
        self._write(f"$ onex {shlex.join(argv)}")
        code = await self._runner.stream(argv, self._write)
        if code:
            self._write(f"exit {code}")
        self.run_worker(self._refresh_status(), exclusive=True, group="status")

    async def _delegate(self, task: str) -> None:
        self._write(f"> {task}")
        self._write("delegating…")
        code, out, err = await self._runner.capture(("delegate", task, "--json"))
        document = _last_json_object(out)
        result = document.get("result") if document else None
        if code or not isinstance(result, dict) or not result.get("response"):
            reason = (_failure_reason(document) if document else None) or (
                err.strip().splitlines()[-1] if err.strip() else "no answer"
            )
            self._write(f"delegation failed: {reason}")
            self._write(f"exit {code}")
            return
        metrics = result.get("metrics")
        metrics = metrics if isinstance(metrics, dict) else {}
        model = str(result.get("model_name") or "unknown model")
        cost = float(metrics.get("cost_usd") or 0.0)
        facts = [model]
        if metrics.get("total_tokens"):
            facts.append(f"{int(metrics['total_tokens']):,} tokens")
        if metrics.get("latency_ms"):
            facts.append(f"{int(metrics['latency_ms']) / 1000:.1f} s")
        facts.append(f"${cost:.4f}")
        if metrics.get("cost_savings_usd"):
            facts.append(f"saved ${float(metrics['cost_savings_usd']):.4f}")
        run_id = str(document.get("run_id") or "") if document else ""
        self.query_one("#output", RichLog).write(
            Markdown(str(result["response"]).strip())
        )
        self._write("  ·  ".join(facts) + (f"  ·  run {run_id[:8]}" if run_id else ""))
        self._last_run = f"last run ${cost:.4f}" + (
            f" · {run_id[:8]}" if run_id else ""
        )
        self._status = self._status.model_copy(update={"model": model})
        self._render_status()


@click.command("tui")
def tui_command() -> None:
    """Open the full-screen onex terminal UI."""
    OnexTui().run()
