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
import re
import shlex
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import ClassVar, Protocol, TypeVar, cast
from urllib.parse import urlparse

import click
from omnibase_core.cli.cli_user_config import user_config_path
from pydantic import BaseModel, ConfigDict, SecretStr
from rich.align import Align
from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding, BindingType
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
    Static,
    Switch,
    Tab,
    Tabs,
)
from textual.widgets.option_list import Option
from textual.worker import Worker

from omnimarket.handlers.local_model_details import read_local_details
from omnimarket.handlers.model_catalog import (
    EMPTY_INDEX,
    ModelsDevIndex,
    catalog_entry,
    load_models_dev,
    stored_entries,
)
from omnimarket.handlers.routing.byok_model_listing import (
    ModelByokListedModel,
    ModelByokModelListing,
    list_byok_models,
)
from omnimarket.models.model_catalog_entry import Deployment, ModelCatalogEntry
from omnimarket.models.model_local_server_details import ModelLocalServerDetails
from omnimarket.nodes.node_onex_setup_effect.cli_model_table import (
    ModelTable,
    entry_key,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_command import (
    HandlerOnexCommand,
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


def _row_label(provider: str, server: str | None = None) -> str:
    """The provider's label, and the server's name when the server said it."""
    if not server:
        return _label(provider)
    return tui_design().model_table.server_label.format(
        provider=_label(provider), server=server
    )


def _say(name: str, **parts: object) -> str:
    """A model screen's sentence from tui_design.yaml, its parts filled in."""
    return tui_design().words[name].format(**parts)


def _on_server(model: str, url: str | None) -> str:
    """A model, with the server it runs on when it was added by URL (OMN-23413)."""
    if not url:
        return model
    return _say("model_on_server", model=model, server=urlparse(url).netloc or url)


def _first_text(provider: str, model: str, url: str | None) -> str:
    """The model tried first, its server and its provider."""
    return _say("first_model", model=_on_server(model, url), provider=_label(provider))


def _hint(provider: str) -> str:
    entry = _entry(provider)
    return entry.hint if entry else ""


def _hosted(provider: str) -> bool:
    return not setup_provider_traits().needs_url(provider)


def _server_defaults() -> set[str]:
    return {_default_url(e.provider) for e in _picker() if e.default_port}


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
        """The command that adds every model after the first, in the order chosen.

        A keyed provider extends its stored key with `models add-model`. A server
        with no key (Ollama, a local server) has no key to extend: it declares all
        of its models, in order, under the endpoint owner setup registered it as,
        with `models add <owner> --url URL --model ...`.
        """
        if len(self.models) < 2:
            return None
        traits = setup_provider_traits()
        if self.url and self.key is None and traits.needs_url(self.provider):
            owner = traits.registration(self.provider, keyed=False)
            every = tuple(part for m in self.models for part in ("--model", m))
            return ("models", "add", owner, "--url", self.url, *every)
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


def _stopped_by(out: str, err: str) -> str:
    """The one step that stopped a setup run, with its fix, else the last line."""
    for document in reversed(_json_rows(out)):
        steps = document.get("observations")
        for step in steps if isinstance(steps, list) else ():
            if isinstance(step, dict) and step.get("status") not in ("success", None):
                fix = str(step.get("fix") or "").split(". The ")[0]
                return f"{step.get('step')} — {fix or step.get('status')}"
    return _last_line(out, err)


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
    return (None if found else str(message or _say("none_found"))), found


ListModels = Callable[[str, str], ModelByokModelListing]
#: Reads the models.dev index (cached under the ONEX home); tests pass a fixture.
LoadIndex = Callable[[], ModelsDevIndex]


def _default_index() -> ModelsDevIndex:
    return load_models_dev(user_config_path().parent)


#: Asks the server at a base URL about its models; tests pass a fixture.
ReadServer = Callable[[str, tuple[str, ...]], dict[str, ModelLocalServerDetails]]


def _default_read_server(
    base: str, models: tuple[str, ...]
) -> dict[str, ModelLocalServerDetails]:
    return read_local_details(base, models)


def _safe_server(
    read: ReadServer, base: str, models: tuple[str, ...]
) -> dict[str, ModelLocalServerDetails]:
    """Supplemental only: a server that does not answer leaves its details unknown."""
    try:
        return read(base, models)
    except Exception:  # the stored list still shows without it
        return {}


def _safe_index(load: LoadIndex) -> ModelsDevIndex:
    """Supplemental only: any failure leaves every enriched field unknown."""
    try:
        return load()
    except Exception:  # the provider's own list still shows without it
        return EMPTY_INDEX


def _listed_entry(
    provider: str, model: ModelByokListedModel, index: ModelsDevIndex
) -> ModelCatalogEntry:
    """A hosted provider's model: the provider's own context and price first."""
    return catalog_entry(
        provider,
        model.id,
        deployment="cloud",
        index=index,
        provider_label=_label(provider),
        name=model.name if model.name != model.id else None,
        context_tokens=model.context_tokens,
        input_usd_per_million=model.input_usd_per_million,
        output_usd_per_million=model.output_usd_per_million,
    )


def _server_entry(
    provider: str,
    model_id: str,
    index: ModelsDevIndex,
    said: ModelLocalServerDetails | None = None,
) -> ModelCatalogEntry:
    """A model a user-run server listed: local, so no API price applies; what the
    server itself said names it and states its context and capabilities."""
    return catalog_entry(
        provider,
        model_id,
        deployment="local",
        index=index,
        provider_label=_row_label(provider, said.server if said else None),
        context_tokens=said.context_tokens if said else None,
        capabilities=said.capabilities if said else None,
    )


_ResultT = TypeVar("_ResultT")


def _tab_strip(current: str) -> Tabs:
    """The same strip on /config, /model and /delegation, the current one lit."""
    strip = Tabs(
        *(Tab(spec.label, id=f"tab-{spec.id}") for spec in tui_design().tabs),
        active=f"tab-{current}",
        id="tabs",
    )
    strip.can_focus = False
    return strip


class _TabbedScreen(ModalScreen[_ResultT]):
    """A screen that is one tab of three: Tab and Shift+Tab move between them."""

    TAB: ClassVar[str] = ""
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("tab", "tab(1)", "Next", priority=True, show=False),
        Binding("shift+tab", "tab(-1)", "Previous", priority=True, show=False),
        # Tab moves between the tabs, so the arrows move between a form's
        # fields; a list or an open picker keeps its own arrows.
        Binding("down", "app.focus_next", "Next field", show=False),
        Binding("up", "app.focus_previous", "Previous field", show=False),
    ]

    def action_tab(self, step: int) -> None:
        order: list[str] = [spec.id for spec in tui_design().tabs]
        self._leave(order[(order.index(self.TAB) + step) % len(order)])

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        target = (event.tab.id or "").removeprefix("tab-")
        if target and target != self.TAB:
            self._leave(target)

    def _leave(self, target: str) -> None:
        app = self.app
        self.dismiss(cast(_ResultT, None))
        if isinstance(app, OnexTui):
            app.call_later(app.open_tab, target)


class SetupScreen(_TabbedScreen[tuple[ModelTuiSetupChoice, ...] | None]):
    """Provider, then its key or URL, then any number of its models; repeat per provider."""

    TAB = "config"

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
        self,
        runner: ProtocolTuiRunner,
        list_models: ListModels | None = None,
        load_index: LoadIndex | None = None,
        read_server: ReadServer | None = None,
    ) -> None:
        super().__init__()
        self._runner = runner
        self._list_models: ListModels = list_models or list_byok_models
        self._load: LoadIndex = load_index or _default_index
        self._read_server: ReadServer = read_server or _default_read_server
        self._basket: dict[str, ModelTuiSetupChoice] = {}
        self._find_timer: Timer | None = None
        self._loading = False

    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield _tab_strip(self.TAB)
            yield Label(_say("config_title"))
            choices = _picker()
            first = next((e for e in choices if e.selected), choices[0])
            yield Select(
                [(e.label, e.provider) for e in choices],
                value=first.provider,
                allow_blank=False,
                id="provider",
            )
            yield Input(placeholder=_say("key_placeholder"), password=True, id="key")
            yield Input(
                value=_default_url(first.provider),
                placeholder=_say("url_placeholder"),
                id="url",
            )
            yield Static("", id="note")
            yield ModelTable(
                selectable=True,
                provider_labels={e.provider: e.label for e in choices},
                id="found",
            )
            yield Static(_say("basket_empty"), id="basket")
            yield Static(tui_design().model_table.help["config"], id="keys")
            with Horizontal(id="buttons"):
                yield Button("Add", id="add")
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#found").display = False
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
        # A default the user has not edited follows the provider; a typed URL stays.
        if not hosted and url.value.strip() in {"", *_server_defaults()}:
            url.value = _default_url(self._provider())
        self.query_one("#key").display = hosted
        self.query_one("#url").display = not hosted

    def on_select_changed(self, event: Select.Changed) -> None:
        self._show_fields()
        self.query_one("#key" if _hosted(self._provider()) else "#url", Input).focus()
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
        note.update(_say("loading", provider=_label(provider)))
        self._loading = True
        try:
            await self._fetch(note, provider, wanted)
        finally:
            self._loading = False

    async def _fetch(
        self, note: Static, provider: str, wanted: tuple[str, str]
    ) -> None:
        if _hosted(provider):
            key = self._value("key")
            if not key:
                note.update(_say("key_first", provider=_label(provider)))
                return
            listing, index = await asyncio.gather(
                asyncio.to_thread(self._list_models, provider, key),
                asyncio.to_thread(self._load_index),
            )
            entries = [_listed_entry(provider, m, index) for m in listing.models]
            message = listing.message
        else:
            (_, out, err), index = await asyncio.gather(
                self._runner.capture(
                    ("models", "discover", "--url", self._value("url"), "--json")
                ),
                asyncio.to_thread(self._load_index),
            )
            message, found = _discovered_models(out or err)
            said = await asyncio.to_thread(
                _safe_server, self._read_server, self._value("url"), tuple(found)
            )
            entries = [
                _server_entry(provider, model, index, said.get(model))
                for model in found
            ]
        if self._source() != wanted:  # the key, URL or provider changed meanwhile
            return
        self._show_rows(entries)
        if entries:
            self.query_one("#found", ModelTable).focus_rows()
        note.update(message or _say("found", count=len(entries)))

    def _load_index(self) -> ModelsDevIndex:
        return _safe_index(self._load)

    def _show_rows(self, entries: list[ModelCatalogEntry]) -> None:
        """New results: forget earlier ticks; the table shows only a read list."""
        table = self.query_one("#found", ModelTable)
        table.set_ticked(())
        table.set_entries(entries)
        table.display = bool(entries)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in ("key", "url"):
            self._schedule_find()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the key or URL field moves on to the list, loading it if needed."""
        if event.input.id not in ("key", "url"):
            return
        table = self.query_one("#found", ModelTable)
        if table.entries:
            table.focus_rows()
        else:
            self.run_worker(self._find(), exclusive=True, group="find")

    def _add_current(self) -> bool:
        provider = self._provider()
        table = self.query_one("#found", ModelTable)
        chosen = [entry.model_id for entry in table.ticked]
        if not chosen:
            return False
        key = self._value("key")
        if _hosted(provider) and not key:
            self.query_one("#note", Static).update(
                _say("needs_key", provider=_label(provider))
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
        table.set_ticked(())
        self.query_one("#basket", Static).update(
            _say(
                "basket",
                choices="   ·   ".join(
                    f"{c.provider}: {', '.join(c.models)}"
                    for c in self._basket.values()
                ),
            )
        )
        return True

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        note = self.query_one("#note", Static)
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "add":
            if self._loading:
                note.update(_say("still_loading"))
            elif self._add_current():
                note.update(_say("added"))
            else:
                note.update(_say("tick_first"))
        elif event.button.id == "save":
            self.action_save()

    def action_save(self) -> None:
        """Save what is ticked or added; refuse with nothing chosen."""
        self._add_current()
        if not self._basket:
            warning = tui_design().delegation.nothing_ticked
            self.query_one("#note", Static).update(
                Text(warning, style=_style("warning"))
            )
            self.app.notify(warning, severity="warning")
            return
        self.dismiss(tuple(self._basket.values()))

    def action_cancel(self) -> None:
        self.dismiss(None)


def _json_rows(text: str) -> list[dict[str, object]]:
    """Every JSON object line of a command's stdout."""
    rows: list[dict[str, object]] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _deployment(provider: str) -> Deployment:
    """A hosted API is cloud; a server the user gives by URL is local."""
    try:
        return "cloud" if _hosted(provider) else "local"
    except Exception:  # a provider the picker does not list runs by URL
        return "local"


class ModelTuiSelection(BaseModel):
    """The models the user selected, in delegation order, as catalogue rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ModelCatalogEntry, ...] = ()
    #: (provider, model) tried first, when one is chosen.
    first: tuple[str, str] | None = None
    #: The server the first model runs on, for a model added by URL.
    first_url: str | None = None
    #: entry key -> connection id, for selected models the gateway can serve.
    gateway: dict[str, str] = {}
    #: entry key -> (connection to register, server URL), for selected models on
    #: a server that answers Anthropic Messages but is not a connection yet.
    connect: dict[str, tuple[str, str]] = {}

    def serves_gateway(self, key: str) -> bool:
        return key in self.gateway or key in self.connect


def server_models(rows: list[dict[str, object]]) -> dict[str, list[tuple[str, str]]]:
    """Base URL -> (provider, model) for every stored model run on a server by URL."""
    found: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        on_servers = row.get("local_models")
        if not row.get("set_up") or not isinstance(on_servers, list):
            continue
        for item in on_servers:
            if isinstance(item, dict) and item.get("model") and item.get("base_url"):
                found.setdefault(str(item["base_url"]), []).append(
                    (str(row["provider"]), str(item["model"]))
                )
    return found


def selected_models(
    rows: list[dict[str, object]],
    connections: list[dict[str, object]],
    index: ModelsDevIndex,
    servers: Mapping[tuple[str, str, str], ModelLocalServerDetails] | None = None,
) -> ModelTuiSelection:
    """`models list --json` rows and `models connections --json` as one table.

    ``servers`` is what each server said about each model, by (provider,
    model, server URL); a model two servers run is two rows (OMN-23413).
    """
    entries = list(
        stored_entries(
            rows,
            index=index,
            deployment_of=_deployment,
            label_of=_row_label,
            servers=servers,
        )
    )
    chosen = next(
        (r for r in rows if r.get("tried_first") and r.get("first_model")), None
    )
    first = (str(chosen["provider"]), str(chosen["first_model"])) if chosen else None
    first_url = str(chosen["first_url"]) if chosen and chosen.get("first_url") else None
    seen = {entry_key(e) for e in entries}
    gateway: dict[str, str] = {}
    for connection in connections:
        provider = str(connection.get("provider") or "")
        models = connection.get("models")
        for model in models if isinstance(models, list) else ():
            entry = catalog_entry(
                provider,
                str(model),
                deployment=_deployment(provider),
                index=index,
                provider_label=_label(provider),
                status="untested",
            )
            key = entry_key(entry)
            if key not in seen:
                entries.append(entry)
                seen.add(key)
            if connection.get("native_messages"):
                gateway[key] = str(connection.get("connection_id"))
    connect: dict[str, tuple[str, str]] = {}
    for entry in entries:
        base = entry.server_url
        said = (
            (servers or {}).get((entry.provider, entry.model_id, base))
            if base
            else None
        )
        key = entry_key(entry)
        if said and said.native_messages and base and key not in gateway:
            connect[key] = (connection_name(said.server or entry.provider, base), base)
    return ModelTuiSelection(
        entries=tuple(entries),
        first=first,
        first_url=first_url,
        gateway=gateway,
        connect=connect,
    )


def connection_name(kind: str, base: str) -> str:
    """A stable connection id for a server: its kind, host and port, e.g.
    ollama-gpu-a-test-11434. The host is in it so two servers of one kind on
    one port (two lab hosts, say) get two names (OMN-23413)."""
    parsed = urlparse(base)
    where = "-".join(str(p) for p in (parsed.hostname, parsed.port) if p)
    name = re.sub(r"[^a-z0-9_-]+", "-", f"{kind}-{where}".lower())
    return (name if name[:1].isalpha() else f"m{name}")[:64]


class ModelTuiDelegationChoice(BaseModel):
    """Claude Code delegation on or off, and the onex commands that do it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    on: bool
    mode: str
    commands: tuple[tuple[str, ...], ...]


class DelegationScreen(_TabbedScreen[ModelTuiDelegationChoice | None]):
    """Your selected models and one switch; nothing is typed (OMN-23413 AC25)."""

    CSS = """
    DelegationScreen { align: center middle; }
    #deleg { width: 90%; max-width: 150; height: 90%; border: round $accent; padding: 0 2; background: $panel; }
    #deleg Select { margin-bottom: 0; }
    #switch-row { height: auto; }
    #switch-row Label { padding: 1 1 0 0; }
    #dnote { color: $text-muted; height: auto; }
    #dkeys { color: $text-muted; height: 1; }
    #dbuttons { height: 3; dock: bottom; align-horizontal: right; }
    """
    TAB = "delegation"
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("space", "flip", "Switch", show=False),
        Binding("m", "next_mode", "Mode", show=False),
    ]

    def __init__(self, selection: ModelTuiSelection, enabled: bool) -> None:
        super().__init__()
        self._selection = selection
        self._enabled = enabled

    def compose(self) -> ComposeResult:
        words = tui_design().delegation
        modes = words.modes
        capable = any(
            self._selection.serves_gateway(k)
            for k in map(entry_key, self._selection.entries)
        )
        start = next(
            (
                m.id
                for m in modes
                if m.needs_gateway_model == (self._enabled and capable)
            ),
            modes[0].id,
        )
        with Vertical(id="deleg"):
            yield _tab_strip(self.TAB)
            yield Label(Text(words.title, style=_style("primary", "bold")))
            yield Select(
                [(m.label, m.id) for m in modes],
                value=start,
                allow_blank=False,
                id="mode",
            )
            yield Static("", id="mdetail")
            with Horizontal(id="switch-row"):
                yield Label(words.switch_label)
                yield Switch(value=self._enabled, id="on")
            yield Label(Text(words.table_title, style=_style("muted")))
            yield ModelTable(
                selectable=False,
                provider_labels={e.provider: e.label for e in _picker()},
                id="selected",
            )
            yield Static("", id="dnote")
            yield Static(tui_design().model_table.help["delegation"], id="dkeys")
            with Horizontal(id="dbuttons"):
                yield Button("Save", variant="primary", id="dsave")
                yield Button("Cancel", id="dcancel")

    def on_mount(self) -> None:
        table = self.query_one("#selected", ModelTable)
        table.set_entries(self._selection.entries)
        table.focus_rows()
        self._describe_mode()
        words = tui_design().delegation
        note = self.query_one("#dnote", Static)
        if not self._selection.entries:
            note.update(Text(words.no_models, style=_style("warning")))
            return
        capable = [
            e.model_id
            for e in self._selection.entries
            if self._selection.serves_gateway(entry_key(e))
        ]
        if capable:
            note.update(f"{words.gateway_marker}: {', '.join(capable)}")

    def _describe_mode(self) -> None:
        wanted = str(self.query_one("#mode", Select).value)
        mode = next(m for m in tui_design().delegation.modes if m.id == wanted)
        self.query_one("#mdetail", Static).update(
            Text(mode.detail, style=_style("muted"))
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        self._describe_mode()

    def action_flip(self) -> None:
        switch = self.query_one("#on", Switch)
        switch.value = not switch.value

    def action_next_mode(self) -> None:
        order = [m.id for m in tui_design().delegation.modes]
        select = self.query_one("#mode", Select)
        select.value = order[(order.index(str(select.value)) + 1) % len(order)]

    def _choice(self) -> ModelTuiDelegationChoice | str:
        """The commands to run, or the reason nothing can be switched."""
        words = tui_design().delegation
        on = self.query_one("#on", Switch).value
        wanted = str(self.query_one("#mode", Select).value)
        mode = next(m for m in words.modes if m.id == wanted)
        fill: dict[str, str] = {}
        first: tuple[tuple[str, ...], ...] = ()
        if on and not self._selection.entries:
            return words.no_models
        if on and mode.needs_gateway_model:
            pick = next(
                (
                    e
                    for e in self._selection.entries
                    if self._selection.serves_gateway(entry_key(e))
                ),
                None,
            )
            if pick is None:
                return words.no_gateway_model
            key = entry_key(pick)
            if key in self._selection.gateway:
                fill = {
                    "connection": self._selection.gateway[key],
                    "model": pick.model_id,
                }
            else:
                name, url = self._selection.connect[key]
                fill = {"connection": name, "model": pick.model_id, "url": url}
                first = (words.connect,)
        steps = (*first, *mode.enable) if on else mode.disable
        return ModelTuiDelegationChoice(
            on=on,
            mode=mode.id,
            commands=tuple(
                tuple(part.format(**fill) for part in step) for step in steps
            ),
        )

    def action_save(self) -> None:
        choice = self._choice()
        if isinstance(choice, str):
            self.query_one("#dnote", Static).update(
                Text(choice, style=_style("warning"))
            )
            return
        self.dismiss(choice)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dcancel":
            self.dismiss(None)
        else:
            self.action_save()

    def action_cancel(self) -> None:
        self.dismiss(None)


class ModelPickScreen(_TabbedScreen[tuple[str, str, str] | None]):
    """Auto, or the model every task tries first, from the one model table."""

    TAB = "model"

    CSS = """
    ModelPickScreen { align: center middle; }
    #pick { width: 90%; max-width: 150; height: 90%; border: round $accent; padding: 0 2; background: $panel; }
    #pnote { color: $text-muted; height: auto; }
    #pkeys { color: $text-muted; height: 1; }
    #auto { width: auto; margin-bottom: 0; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("a", "auto", "Auto", show=False),
    ]

    def __init__(self, selection: ModelTuiSelection) -> None:
        super().__init__()
        self._selection = selection

    def compose(self) -> ComposeResult:
        words = tui_design().model_table
        first = self._selection.first
        current = (
            _first_text(first[0], first[1], self._selection.first_url)
            if first
            else words.auto_label
        )
        with Vertical(id="pick"):
            yield _tab_strip(self.TAB)
            yield Label(
                Text(
                    _say("model_title"),
                    style=_style("primary", "bold"),
                )
            )
            yield Static(
                Text(_say("model_now", current=current), style=_style("success")),
                id="pnow",
            )
            yield Button(words.auto_label + "  (A)", id="auto")
            yield ModelTable(
                selectable=False,
                provider_labels={e.provider: e.label for e in _picker()},
                id="models",
            )
            yield Static("", id="pnote")
            yield Static(words.help["model"], id="pkeys")

    def on_mount(self) -> None:
        table = self.query_one("#models", ModelTable)
        table.set_entries(self._selection.entries)
        table.query_one("DataTable").focus()

    @on(ModelTable.Chosen)
    def _chosen(self, event: ModelTable.Chosen) -> None:
        entry = event.entry
        if entry.status in ("failed", "unavailable"):
            reason = (
                entry.status_reason
                or tui_design().model_table.status[entry.status].text
            )
            self.query_one("#pnote", Static).update(
                Text(
                    _say("cannot_be_first", model=entry.model_id, reason=reason),
                    style=_style("warning"),
                )
            )
            return
        self.dismiss((entry.provider, entry.model_id, entry.server_url or ""))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "auto":
            self.action_auto()

    def action_auto(self) -> None:
        self.dismiss(("", "", ""))

    def action_cancel(self) -> None:
        self.dismiss(None)


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
        load_index: LoadIndex | None = None,
        read_server: ReadServer | None = None,
    ) -> None:
        super().__init__()
        self._load_index: LoadIndex = load_index or _default_index
        self._read_server: ReadServer = read_server or _default_read_server
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
        self._commands = tuple(
            c for c in command_tree(root) if not tui_design().replaces(c.path)
        )
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
        if key == "tab" and isinstance(self.screen, _TabbedScreen):
            self.screen.action_tab(1)  # /config, /model, /delegation: next tab
        elif key == "tab":
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
        if entry.path[:1] in (("models",), ("claude",)):
            self.run_worker(self._refresh_status(), exclusive=True, group="status")

    async def open_tab(self, name: str) -> None:
        """Open /config, /model or /delegation, as Tab on one of them asks."""
        await self._shortcut(name)

    async def _shortcut(self, action: str) -> None:
        if action == "quit":
            self.exit(0)
        elif action == "help":
            self._write(Text(tui_design().help, style=_style("foreground")))
        elif action == "clear":
            self.query_one("#output", RichLog).clear()
        elif action == "config":
            self.push_screen(
                SetupScreen(
                    self._runner, self._list_models, self._load_index, self._read_server
                ),
                self._apply_setup,
            )
        elif action == "model":
            await self._pick_model()
        elif action == "delegation":
            selection = await self._load_selection()
            self.push_screen(
                DelegationScreen(selection, self._status.gateway),
                self._apply_delegation,
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
            ready = list(choice.models[:1])
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
                if not code:
                    ready = list(choice.models)
            if ok:
                self._write(
                    Text(
                        f"Ready: {_label(choice.provider)} · {', '.join(ready)}. "
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

    async def _load_selection(self) -> ModelTuiSelection:
        """The selected models, read through the CLI's own JSON (no second engine)."""
        (_, listed, _), (_, connected, _) = await asyncio.gather(
            self._runner.capture(("models", "list", "--json")),
            self._runner.capture(("models", "connections", "--json")),
        )
        documents = _json_rows(connected)
        connections = documents[-1].get("connections") if documents else None
        rows = _json_rows(listed)
        by_server = server_models(rows)
        index, *answers = await asyncio.gather(
            asyncio.to_thread(_safe_index, self._load_index),
            *(
                asyncio.to_thread(
                    _safe_server,
                    self._read_server,
                    base,
                    tuple(model for _, model in pairs),
                )
                for base, pairs in by_server.items()
            ),
        )
        servers = {
            (provider, model, base): said[model]
            for (base, pairs), said in zip(by_server.items(), answers, strict=True)
            for provider, model in pairs
            if model in said
        }
        return selected_models(
            rows,
            [c for c in connections if isinstance(c, dict)]
            if isinstance(connections, list)
            else [],
            index,
            servers,
        )

    async def _apply_delegation(self, choice: ModelTuiDelegationChoice | None) -> None:
        if choice is None:
            return
        glyphs = tui_design().glyphs
        words = tui_design().delegation
        state = "on" if choice.on else "off"
        for argv in choice.commands:
            code, out, err = await self._runner.capture(argv)
            if code:
                reason = _stopped_by(out, err)
                self._write(
                    Text(
                        f"{glyphs.failed} "
                        + words.not_switched.format(state=state, reason=reason),
                        style=_style("error"),
                    )
                )
                break
        else:
            mode = next(m for m in words.modes if m.id == choice.mode)
            done = (
                words.switched_on.format(mode=mode.label)
                if choice.on
                else words.switched_off
            )
            self._write(Text(f"{glyphs.done} {done}", style=_style("success")))
        self.run_worker(self._refresh_status(), exclusive=True, group="status")

    async def _pick_model(self) -> None:
        selection = await self._load_selection()
        if not selection.entries:
            self._write(_say("nothing_set_up"))
            return
        self.push_screen(ModelPickScreen(selection), self._apply_model)

    async def _apply_model(self, picked: tuple[str, str, str] | None) -> None:
        if picked is None:
            return
        provider, model, url = picked
        auto = not provider
        argv: tuple[str, ...] = (
            ("models", "first", "--clear")
            if auto
            else ("models", "first", provider, model, *(("--url", url) if url else ()))
        )
        code, out, err = await self._runner.capture(argv)
        glyphs = tui_design().glyphs
        chosen = (
            tui_design().model_table.auto_label
            if auto
            else _say(
                "now_first", model=_on_server(model, url), provider=_label(provider)
            )
        )
        self._write(
            Text(f"{glyphs.done} {chosen}", style=_style("success"))
            if not code
            else Text(
                f"{glyphs.failed} " + _say("first_failed", reason=_last_line(out, err)),
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
