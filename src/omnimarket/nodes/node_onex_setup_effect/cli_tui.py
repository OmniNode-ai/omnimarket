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
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

import click
from pydantic import BaseModel, ConfigDict, SecretStr
from rich.align import Align
from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.timer import Timer
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
from textual.worker import Worker

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
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
    tui_design,
)

_PROMPTING_OPTIONS = frozenset(("interactive", "yes"))


def _shortcut_paths() -> dict[tuple[str, ...], str]:
    """The design file's shortcuts, in order: path -> one-line help."""
    return {s.path: s.help for s in tui_design().shortcuts}


def _alias_paths() -> set[tuple[str, ...]]:
    """Typed-only names (an alias) never listed in the palette."""
    return {(alias,) for alias in tui_design().aliases}


def _style(token: str, *extra: str) -> str:
    """A Rich style from a design colour token, e.g. ``_style("error", "bold")``."""
    return " ".join((*extra, getattr(tui_design().theme, token)))


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

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]: ...

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


def _stop(process: asyncio.subprocess.Process) -> None:
    """End a cancelled onex run so it does not keep working in the background."""
    if process.returncode is None:
        process.kill()


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
        try:
            async for raw in process.stdout:
                emit(raw.decode(errors="replace").rstrip("\n"))
        except asyncio.CancelledError:
            _stop(process)
            raise
        return await process.wait()

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            self._onex,
            *argv,
            stdin=asyncio.subprocess.DEVNULL
            if stdin is None
            else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await process.communicate(
                None if stdin is None else stdin.encode()
            )
        except asyncio.CancelledError:
            _stop(process)
            raise
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


def _setup_report(text: str) -> dict[str, object] | None:
    """`onex setup --json`: one JSON document on stdout."""
    try:
        value = json.loads(text)
    except ValueError:
        return _last_json_object(text)
    return value if isinstance(value, dict) else None


def _last_line(out: str, err: str) -> str:
    lines = [ln for ln in (err or out).strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "no output"


def _step_detail(evidence: dict[str, object]) -> str:
    """The one evidence value worth a glance: a folder, or what a model step set."""
    for key in ("workspace",):
        if evidence.get(key):
            return f"  {display_path(Path(str(evidence[key])))}"
    picked = [
        f"{k}: {str(v).split(';')[0]}" for k, v in evidence.items() if ";" in str(v)
    ]
    return f"  {', '.join(picked)}" if picked else ""


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
    #form { width: 90%; max-width: 140; height: 90%; border: round $accent; padding: 0 2; background: $panel; }
    #form Input, #form Select { margin-bottom: 0; }
    #found { height: 1fr; min-height: 3; display: none; }
    #note { color: $text-muted; height: auto; }
    #basket { color: $success; height: auto; }
    #keys { color: $text-muted; height: 1; }
    #buttons { height: 3; dock: bottom; align-horizontal: right; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Back"),
        Binding("ctrl+s", "save", "Save", priority=True),
    ]

    def __init__(
        self, runner: ProtocolTuiRunner, list_models: ListModels | None = None
    ) -> None:
        super().__init__()
        self._runner = runner
        self._list_models: ListModels = list_models or list_byok_models
        self._basket: dict[str, ModelTuiSetupChoice] = {}
        self._rows: list[tuple[str, str]] = []
        self._ticked: set[str] = set()
        self._find_timer: Timer | None = None

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
            yield Static("", id="note")
            yield Static("Selected: none yet (at least one is needed)", id="basket")
            yield Static(
                "Space ticks a model  ·  Add keeps this provider's picks  ·  "
                "Ctrl+S saves  ·  Esc goes back",
                id="keys",
            )
            with Horizontal(id="buttons"):
                yield Button("Find models", id="find")
                yield Button("Add", id="add")
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#filter").display = False
        self._show_fields()
        self.query_one("#note", Static).update(_hint(self._provider()))
        self._schedule_find()

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
        self._schedule_find()

    def _source(self) -> tuple[str, str]:
        """What a model list depends on: the provider and its key or URL."""
        provider = self._provider()
        return provider, self._value("key" if _hosted(provider) else "url")

    def _schedule_find(self) -> None:
        """Fetch the list on its own once the key or URL stops changing."""
        if self._find_timer is not None:
            self._find_timer.stop()
        self._find_timer = self.set_timer(
            tui_design().auto_find_seconds, self._auto_find
        )

    def _auto_find(self) -> None:
        provider, value = self._source()
        if not value or (
            _hosted(provider) and len(value) < tui_design().min_key_length
        ):
            return
        self.run_worker(self._find(), exclusive=True, group="find")

    async def _find(self) -> None:
        note = self.query_one("#note", Static)
        provider = self._provider()
        wanted = self._source()
        note.update(f"loading {_label(provider)} models…")
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
        if self._source() != wanted:  # the key, URL or provider changed meanwhile
            return
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
            listing.add_options(shown)
        listing.display = bool(self._rows)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._render_rows()
        elif event.input.id in ("key", "url"):
            self._schedule_find()

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
            self.action_save()

    def action_save(self) -> None:
        """Save what is ticked or added; refuse with nothing chosen."""
        self._add_current()
        if not self._basket:
            self.query_one("#note", Static).update(
                "select at least one model before saving"
            )
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
                        f"{model}  ·  {provider}"
                        + (f"  {tui_design().glyphs.done} current" if first else ""),
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


def _big_word(word: str, gradient: tuple[str, ...]) -> Text:
    """``word`` in the design font, coloured left to right along ``gradient``."""
    font = tui_design().font
    rows = ["  ".join(font[c][r] for c in word) for r in range(3)]
    width = max(len(row) for row in rows)
    block = Text(no_wrap=True)
    for index, row in enumerate(rows):
        if index:
            block.append("\n")
        for column, char in enumerate(row.ljust(width)):
            block.append(
                char, style=f"bold {gradient[column * len(gradient) // width]}"
            )
    return block


def welcome_sections() -> list[Text]:
    """Greeting, banner words, tagline and tips, each its own block."""
    design = tui_design()
    sections = (
        [Text(design.banner.greeting, style=_style("muted", "italic"))]
        if design.banner.greeting
        else []
    )
    sections += [
        _big_word(word.text, design.gradients[word.gradient])
        for word in design.banner.words
    ]
    sections.append(Text(design.banner.tagline, style=_style("muted", "italic")))
    key_width = max(len(tip.key) for tip in design.banner.tips) + 3
    tips = Text(no_wrap=True)
    for index, tip in enumerate(design.banner.tips):
        if index:
            tips.append("\n")
        tips.append(f"{design.glyphs.tip} ", style=_style("primary"))
        tips.append(f"{tip.key:<{key_width}}", style=_style("secondary", "bold"))
        tips.append(tip.text, style=_style("foreground"))
    sections.append(tips)
    return sections


def welcome_banner() -> Text:
    """The welcome sections as one plain block (what the screen shows, uncentred)."""
    return Text("\n\n").join(welcome_sections())


def welcome_renderable() -> Group:
    """Each section centred as a block, so letters and tips stay aligned inside."""
    parts: list[Align | Text] = [Text("")]
    for section in welcome_sections():
        parts += [Align.center(section), Text("")]
    return Group(*parts)


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
        Binding("ctrl+c", "interrupt", "Cancel or quit", priority=True),
        Binding("escape", "escape", "Close or cancel", show=False),
        Binding("tab", "complete", "Complete", show=False, priority=True),
        Binding("down", "palette_down", show=False),
        Binding("up", "palette_up", show=False),
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
        tokens = tui_design().theme
        self.register_theme(
            Theme(
                name=tokens.name,
                dark=tokens.dark,
                primary=tokens.primary,
                secondary=tokens.secondary,
                accent=tokens.accent,
                success=tokens.success,
                warning=tokens.warning,
                error=tokens.error,
                foreground=tokens.foreground,
                background=tokens.background,
                surface=tokens.surface,
                panel=tokens.panel,
            )
        )
        self.theme = tokens.name
        if root is None:
            from omnibase_core.cli.cli_commands import cli

            root = cli
        self._commands = command_tree(root)
        self._runner: ProtocolTuiRunner = runner or HandlerOnexCommand()
        self._list_models = list_models
        self._load_status = load_status or globals()["load_status"]
        self._status = ModelTuiStatus(workspace=display_path(Path.cwd()))
        self._last_run = ""
        self._active_task: Worker[None] | None = None
        self._task_started = 0.0
        self._ticker: Timer | None = None
        self._history: list[str] = []
        self._recall = 0

    def compose(self) -> ComposeResult:
        yield RichLog(id="output", wrap=True, markup=False, highlight=False)
        yield OptionList(id="palette")
        yield Input(
            id="prompt",
            placeholder=tui_design().prompt_placeholder,
        )
        yield Static(id="status")

    async def on_mount(self) -> None:
        self.query_one("#output", RichLog).write(welcome_renderable(), expand=True)
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
        sep = ("  ·  ", _style("muted"))
        parts: list[tuple[str, str]] = [
            (self._status.workspace, _style("muted")),
            sep,
            (f"model {self._status.model or 'not set'}", _style("secondary", "bold")),
            sep,
            (
                ("gateway on", _style("success"))
                if self._status.gateway
                else ("gateway off", _style("muted"))
            ),
        ]
        if self._last_run:
            parts += [sep, (self._last_run, _style("accent"))]
        if self._active_task is not None:
            seconds = int(time.monotonic() - self._task_started)
            parts += [
                sep,
                (f"working {seconds}s  ·  esc cancels", _style("warning", "bold")),
            ]
        else:
            parts += [sep, ("/ commands  ctrl+c quit", _style("muted"))]
        self.query_one("#status", Static).update(Text.assemble(*parts))

    def _write(self, text: str | Text) -> None:
        self.query_one("#output", RichLog).write(text)

    def _matches(self, query: str) -> list[ModelTuiCommand]:
        words = query.lower().split()
        local = [
            ModelTuiCommand(path=path, help=text, needs_terminal=False)
            for path, text in _shortcut_paths().items()
        ]
        return [
            entry
            for entry in (
                *local,
                *(
                    c
                    for c in self._commands
                    if c.path not in _shortcut_paths() and c.path not in _alias_paths()
                ),
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

    def action_escape(self) -> None:
        """Esc closes the palette, or else cancels the running task."""
        palette = self.query_one("#palette", OptionList)
        if palette.display:
            palette.display = False
        elif self._active_task is not None:
            self._active_task.cancel()

    def action_interrupt(self) -> None:
        """Ctrl+C cancels the running task; with nothing running it quits."""
        if self._active_task is not None:
            self._active_task.cancel()
        else:
            self.exit(0)

    def _start_task(self, work: Awaitable[None]) -> None:
        self._task_started = time.monotonic()
        self._ticker = self.set_interval(1, self._render_status)
        self._active_task = self.run_worker(
            self._guarded(work), group="task", exclusive=True, exit_on_error=False
        )
        self._render_status()

    async def _guarded(self, work: Awaitable[None]) -> None:
        try:
            await work
        except asyncio.CancelledError:
            seconds = int(time.monotonic() - self._task_started)
            self._write(Text(f"cancelled after {seconds}s", style=_style("muted")))
            raise
        finally:
            self._active_task = None
            if self._ticker is not None:
                self._ticker.stop()
            self._render_status()

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

    def _palette_choice(self, text: str) -> str:
        """Enter on a partly typed command runs the highlighted palette entry."""
        palette = self.query_one("#palette", OptionList)
        if not (text.startswith("/") and palette.display and " " not in text):
            return text
        words = text[1:].split()
        exact = (
            tui_design().shortcut_for(words) is not None
            or self._resolve(words) is not None
        )
        if exact or palette.highlighted is None:
            return text
        return f"/{palette.get_option_at_index(palette.highlighted).id}"

    def _resolve(self, words: list[str]) -> ModelTuiCommand | None:
        known = {entry.path: entry for entry in self._commands}
        for size in range(len(words), 0, -1):
            entry = known.get(tuple(words[:size]))
            if entry is not None:
                return entry
        return None

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = self._palette_choice(event.value.strip())
        prompt = self.query_one("#prompt", Input)
        prompt.value = ""
        self.action_close_palette()
        if not text:
            return
        self._history.append(text)
        self._recall = len(self._history)
        if not text.startswith("/"):
            if self._active_task is not None:
                self._write(
                    Text(
                        "a task is still running: Esc or Ctrl+C cancels it",
                        style=_style("warning"),
                    )
                )
                return
            self._start_task(self._delegate(text))
            return
        try:
            words = shlex.split(text[1:])
        except ValueError as error:
            self._write(f"could not read that command: {error}")
            return
        shortcut = tui_design().shortcut_for(words)
        if shortcut is not None:
            await self._shortcut(shortcut.action)
            return
        entry = self._resolve(words)
        if entry is None:
            self._write(
                f"no onex command matches '/{' '.join(words)}'; type / to list them"
            )
            return
        await self._run(entry, tuple(words))

    async def _run(self, entry: ModelTuiCommand, argv: tuple[str, ...]) -> None:
        self._write(Text(f"$ onex {shlex.join(argv)}", style=_style("muted")))
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

    async def _shortcut(self, action: str) -> None:
        if action == "quit":
            self.exit(0)
        elif action == "help":
            self._write(Text(tui_design().help, style=_style("foreground")))
        elif action == "clear":
            self.query_one("#output", RichLog).clear()
        elif action == "config":
            self.push_screen(
                SetupScreen(self._runner, self._list_models), self._apply_setup
            )
        elif action == "model":
            await self._pick_model()
        elif action == "delegation":
            self.push_screen(
                DelegationScreen(self._status.gateway), self._apply_delegation
            )

    async def _apply_setup(
        self, choices: tuple[ModelTuiSetupChoice, ...] | None
    ) -> None:
        for choice in choices or ():
            models = ", ".join(choice.models)
            self._write(
                Text(
                    f"Setting up {_label(choice.provider)} · {models} …",
                    style=_style("foreground", "bold"),
                )
            )
            stdin = choice.key.get_secret_value() + "\n" if choice.key else None
            code, out, err = await self._runner.capture(
                (*choice.argv(Path.cwd()), "--json"), stdin=stdin
            )
            ok = self._write_setup_report(_setup_report(out), code, err)
            more = choice.more_models_argv()
            if ok and more:
                code, out, err = await self._runner.capture(more)
                extra = ", ".join(choice.models[1:])
                self._write(
                    Text(
                        f"  {tui_design().glyphs.done} also added {extra}",
                        style=_style("success"),
                    )
                    if not code
                    else Text(
                        f"  {tui_design().glyphs.failed} adding {extra}: {_last_line(out, err)}",
                        style=_style("error"),
                    )
                )
            if ok:
                self._write(
                    Text(
                        f"Ready: {_label(choice.provider)} · {models}. "
                        "Type a task to try it, or /model to choose which goes first.",
                        style=_style("success", "bold"),
                    )
                )
        self.run_worker(self._refresh_status(), exclusive=True, group="status")

    def _write_setup_report(
        self, report: dict[str, object] | None, code: int, err: str
    ) -> bool:
        """One line per setup step: done, unchanged, or failed with its fix."""
        steps = report.get("observations") if report else None
        if not isinstance(steps, list):
            self._write(
                Text(
                    f"  {tui_design().glyphs.failed} setup failed: {_last_line('', err)}",
                    style=_style("error"),
                )
            )
            return False
        ok = code == 0
        for step in steps:
            if not isinstance(step, dict):
                continue
            name = str(step.get("step", "")).replace("_", " ").replace("-", " ")
            evidence = step.get("evidence")
            detail = _step_detail(evidence if isinstance(evidence, dict) else {})
            if step.get("status") == "success":
                mark, style = (
                    (tui_design().glyphs.done, _style("success"))
                    if step.get("changed")
                    else (tui_design().glyphs.unchanged, _style("muted"))
                )
                self._write(Text(f"  {mark} {name}{detail}", style=style))
            else:
                ok = False
                fix = step.get("fix")
                self._write(
                    Text(
                        f"  {tui_design().glyphs.failed} {name}{detail}"
                        + (f": {fix}" if fix else ""),
                        style=_style("error"),
                    )
                )
        return ok

    async def _apply_delegation(self, choice: ModelTuiDelegationChoice | None) -> None:
        if choice is None:
            return
        argv = choice.argv()
        self._write(Text(f"$ onex {shlex.join(argv)}", style=_style("muted")))
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
        provider, model = picked
        code, out, err = await self._runner.capture(
            ("models", "first", provider, model)
        )
        glyphs = tui_design().glyphs
        self._write(
            Text(
                f"{glyphs.done} {model} is now tried first ({_label(provider)})",
                style=_style("success"),
            )
            if not code
            else Text(
                f"{glyphs.failed} could not make {model} first: {_last_line(out, err)}",
                style=_style("error"),
            )
        )
        self.run_worker(self._refresh_status(), exclusive=True, group="status")

    async def _delegate(self, task: str) -> None:
        self._write(
            Text(
                f"{tui_design().glyphs.task} {task}", style=_style("secondary", "bold")
            )
        )
        self._write(Text("delegating…", style=_style("muted", "italic")))
        code, out, err = await self._runner.capture(("delegate", task, "--json"))
        document = _last_json_object(out)
        result = document.get("result") if document else None
        if code or not isinstance(result, dict) or not result.get("response"):
            reason = (_failure_reason(document) if document else None) or (
                err.strip().splitlines()[-1] if err.strip() else "no answer"
            )
            self._write(
                Text(
                    f"{tui_design().glyphs.failed} delegation failed: {reason}",
                    style=_style("error"),
                )
            )
            self._write(Text(f"exit {code}", style=_style("muted")))
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
        self._write(
            Text(
                "  ·  ".join(facts) + (f"  ·  run {run_id[:8]}" if run_id else ""),
                style=_style("muted"),
            )
        )
        self._last_run = f"last run ${cost:.4f}" + (
            f" · {run_id[:8]}" if run_id else ""
        )
        self._status = self._status.model_copy(update={"model": model})
        self._render_status()


@click.command("tui")
def tui_command() -> None:
    """Open the full-screen onex terminal UI."""
    OnexTui().run()
