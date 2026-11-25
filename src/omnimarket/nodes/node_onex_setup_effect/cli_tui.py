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
import contextlib
import importlib.metadata
import json
import logging
import re
import shlex
import signal
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, TypeVar, cast
from urllib.parse import urlparse

import click
from omnibase_core.cli.cli_user_config import user_config_path
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual import events, on
from textual.app import (
    App,
    ComposeResult,
    RenderResult,
    SuspendNotSupported,
    SystemCommand,
)
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.timer import Timer
from textual.widgets import (
    Button,
    DataTable,
    Input,
    OptionList,
    RadioButton,
    RadioSet,
    Static,
    Switch,
    Tab,
    Tabs,
)
from textual.widgets._tabs import Underline
from textual.widgets.option_list import Option
from textual.worker import Worker

from omnimarket.handlers.local_model_details import read_local_details
from omnimarket.handlers.model_catalog import (
    EMPTY_INDEX,
    ModelsDevIndex,
    catalog_config,
    catalog_entry,
    list_and_remember_models,
    listed_catalog_entry,
    load_models_dev,
    stored_entries,
)
from omnimarket.handlers.models_cli_output import connection_rows, model_status_rows
from omnimarket.handlers.routing.byok_model_listing import (
    ModelByokListedModel,
    ModelByokModelListing,
)
from omnimarket.models.model_catalog_entry import Deployment, ModelCatalogEntry
from omnimarket.models.model_local_server_details import ModelLocalServerDetails
from omnimarket.nodes.node_onex_setup_effect.cli_model_table import (
    ModelTable,
    entry_key,
)
from omnimarket.nodes.node_onex_setup_effect.doctor_page import (
    DoctorPage,
    render_doctor_page,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_command import (
    HandlerOnexCommand,
)
from omnimarket.nodes.node_onex_setup_effect.metering_page import (
    MeteringPage,
    render_metering_page,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
    ModelSetupPickerProvider,
    setup_provider_traits,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
    ModelTuiFooterKey,
    tui_design,
)
from omnimarket.nodes.node_onex_setup_effect.plain_messages import (
    plain_message,
)

_PROMPTING_OPTIONS = frozenset(("interactive", "yes"))


def _shortcut_paths() -> dict[tuple[str, ...], str]:
    """The design file's shortcuts, in order: path -> one-line help."""
    return {s.path: s.help for s in tui_design().shortcuts}


def _palette_row(entry: ModelTuiCommand) -> Text:
    """One palette row: icon, /name, short hint (all from the design)."""
    design = tui_design()
    name = entry.path[0]
    row = Text(no_wrap=True, overflow="ellipsis")
    row.append(f"{icon(f'cmd_{name}'):<2} ", style=_style("primary"))
    row.append(
        f"/{' '.join(entry.path)}".ljust(design.palette.name_width),
        style=_style("foreground", "bold"),
    )
    row.append(design.palette.hints.get(name, entry.help), style=_style("muted"))
    return row


def _is_customer_command(path: tuple[str, ...]) -> bool:
    """Whether ``path`` is one of the customer commands or a subcommand of one.

    `/delegation on` runs because `delegation` is a customer command.
    """
    return any(
        path[: len(command)] == command for command in tui_design().customer_commands
    )


def _alias_paths() -> set[tuple[str, ...]]:
    """Typed-only names (an alias) never listed in the palette."""
    return {(alias,) for alias in tui_design().aliases}


_LOG = logging.getLogger(__name__)


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


class ModelTuiCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: tuple[str, ...]
    help: str
    needs_terminal: bool

    @property
    def label(self) -> str:
        return "/" + " ".join(self.path)


#: Claude Code delegation as `onex delegation status` read it; "unknown" when
#: the read failed (review 2026-11-15).
DelegationState = Literal["off", "hook", "unknown"]

#: The command groups whose palette commands change what the status bar shows,
#: so it is read again after them (review 2026-11-15: `/delegation on` left
#: the bar and the next /delegation tab on the state from before).
_STATE_GROUPS: tuple[tuple[str, ...], ...] = (("models",), ("delegation",), ("hooks",))
#: Of those, the ones that switch delegation: its state is unread until then.
_SWITCH_GROUPS: tuple[tuple[str, ...], ...] = (("delegation",), ("hooks",))


class ModelTuiStatus(BaseModel):
    """What the status bar shows, read from the CLI (OMN-23413)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The model tried first; ``None`` when Auto picks per task.
    model: str | None = None
    #: The server the first model runs on, for a model added by URL.
    first_url: str | None = None
    #: How many models are set up; ``None`` until the setup is read.
    models: int | None = None
    #: Claude Code delegation as it is: off or on (the hooks), "unknown" when
    #: the read failed, ``None`` until it is read. No folder: delegation is
    #: system-wide (OMN-23413).
    delegation: DelegationState | None = None
    #: The delegation hook as `onex delegation status` read it: prompt_hook,
    #: "installed", "missing" or "paused"; empty until it is read.
    hooks: dict[str, str] = Field(default_factory=dict)
    #: Why the delegation read failed; ``None`` when it was read.
    problem: str | None = None


def _delegation_state(hook: str | None) -> DelegationState:
    """The state `onex delegation status` reports: a failed read is neither on
    nor off (review 2026-11-15: drawn as OFF, an untouched Save switched off)."""
    if hook == "on":
        return "hook"
    return "off" if hook == "off" else "unknown"


def _delegation_read(
    code: int, text: str
) -> tuple[DelegationState, dict[str, str], str | None]:
    """(state, hooks, why it failed) from `onex delegation status --json` output.

    A non-zero exit, no status line or an unknown status is "unknown", never off.
    """
    document = _last_json_object(text)
    status = document.get("status") if document is not None else None
    state = _delegation_state(status if isinstance(status, str) else None)
    if code or document is None or state == "unknown":
        why = _say("exit_code", code=code) if code else _say("status_no_line")
        return ("unknown", {}, f"{why}: {_last_line(text, '')}")
    return (state, _with_switches(_hook_presence(document), document), None)


def _with_switches(
    hooks: dict[str, str], document: dict[str, object]
) -> dict[str, str]:
    """An installed hook whose own switch is off reads "paused" (status's
    "switches", operator 2026-11-15: "build separate switches")."""
    switches = document.get("switches")
    if not isinstance(switches, dict):
        return hooks
    merged = dict(hooks)
    for hook in tui_design().delegation.hooks:
        if (
            switches.get(hook.switch) == "paused"
            and merged.get(hook.key) == "installed"
        ):
            merged[hook.key] = "paused"
    return merged


def _count(n: int, noun: str) -> str:
    """``1 model``, ``2 models``."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


class ModelTuiSetupChoice(BaseModel):
    """One provider and the models chosen for it; the key never reaches argv or the log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    url: str | None
    models: tuple[str, ...]
    key: SecretStr | None = None

    def models_argv(self, stored: tuple[str, ...] = ()) -> tuple[str, ...]:
        """The one `onex models add` that stores these models (OMN-23413).

        A server with no key (a local server) appends them after the models
        already declared there. A key is stored with its whole ordered list,
        so the models it already has (``stored``) come first and none is
        dropped. The key itself goes on stdin, never into argv.
        """
        traits = setup_provider_traits()
        if self.url and self.key is None and traits.needs_url(self.provider):
            owner = traits.registration(self.provider, keyed=False)
            every = tuple(part for m in self.models for part in ("--model", m))
            return ("models", "add", owner, "--url", self.url, *every)
        if self.key is None:
            extra = tuple(part for m in self.models for part in ("--model", m))
            return ("models", "add-model", self.provider, *extra)
        wanted = tuple(dict.fromkeys((*stored, *self.models)))
        every = tuple(part for m in wanted for part in ("--model", m))
        return ("models", "add", self.provider, *every)


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
    """Default model and delegation state, read through the CLI's own JSON output."""
    runner = HandlerOnexCommand()
    lines: list[str] = []
    connected: list[str] = []
    listed_code, connected_code = await asyncio.gather(
        runner.stream(("models", "list", "--json"), lines.append),
        runner.stream(("models", "connections", "--json"), connected.append),
    )
    if listed_code != 0:
        raise RuntimeError("models list could not be read")
    if connected_code != 0:
        raise RuntimeError("models connections could not be read")
    try:
        rows = model_status_rows(lines)
    except ValueError as error:
        raise RuntimeError("models list returned invalid model status") from error
    try:
        connection_status, connection_list = connection_rows("\n".join(connected))
    except ValueError as error:
        raise RuntimeError(
            "models connections returned invalid configuration"
        ) from error
    if connection_status != "ready":
        raise RuntimeError("models connections could not be read")
    # The footer counts the same selection as the tables. Supplemental model
    # metadata is unnecessary for this count and must not trigger a network read.
    selection = selected_models(
        rows,
        connection_list,
        EMPTY_INDEX,
    )
    hook: list[str] = []
    # The hook read asks Claude Code (about 3 s, 7 s measured 2026-11-15).
    code = await runner.stream(tui_design().delegation.claude_check, hook.append)
    delegation, hooks, problem = _delegation_read(code, "\n".join(hook))
    return ModelTuiStatus(
        model=selection.first[1] if selection.first else None,
        first_url=selection.first_url,
        models=len(selection.entries),
        delegation=delegation,
        hooks=hooks,
        problem=problem,
    )


def _hook_presence(doc: object) -> dict[str, str]:
    """prompt_hook from status's claude-hooks evidence (OMN-23413: one hook)."""
    observations = doc.get("observations") if isinstance(doc, dict) else None
    for observation in observations if isinstance(observations, list) else ():
        evidence = (
            observation.get("evidence") if isinstance(observation, dict) else None
        )
        if isinstance(evidence, dict) and observation.get("step") == "claude-hooks":
            return {
                key: str(evidence[key]) for key in ("prompt_hook",) if key in evidence
            }
    return {}


def _measured_cost(metrics: dict[str, object]) -> float | None:
    """The run's cost in USD, or None when it was not measured: no cost_usd, or a
    zero the backend marks cost_measured false, which means unknown, not free."""
    value = metrics.get("cost_usd")
    if not isinstance(value, int | float):
        return None
    if not value and metrics.get("cost_measured") is False:
        return None
    return float(value)


def _last_line(out: str, err: str) -> str:
    lines = [ln for ln in (err or out).strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "no output"


def claude_code_state(document: dict[str, object] | None) -> tuple[str, str]:
    """(state, fix) from `onex delegation status --json`: not_installed,
    signed_out, ready, installed, or unknown (OMN-23413). The CLI's top-level
    claude_code is used when it names a state (audit S9-14); an older document
    without it is read from the claude-plugin step."""
    if document is None:
        return ("unknown", "")
    named = document.get("claude_code")
    if named in ("not_installed", "signed_out", "ready", "installed"):
        return (str(named), str(document.get("fix") or ""))
    steps = document.get("observations")
    step = next(
        (
            o
            for o in (steps if isinstance(steps, list) else [])
            if isinstance(o, dict) and o.get("step") == "claude-plugin"
        ),
        None,
    )
    if step is None:
        return ("unknown", "")
    fix = str(step.get("fix") or "")
    marked = (step.get("evidence") or {}).get("claude_code")
    if marked in ("not_installed", "signed_out"):
        return (str(marked), fix)
    if step.get("status") == "success":
        return ("ready", "")
    return ("installed", fix)


def _stopped_by(out: str, err: str) -> str:
    """The one step that stopped a setup run, with its fix, else the last line."""
    if _SETUP_LOCK in out or _SETUP_LOCK in err:  # say what to do, not the error
        return _say("setup_busy")
    for document in reversed(_json_rows(out)):
        steps = document.get("observations")
        for step in steps if isinstance(steps, list) else ():
            if isinstance(step, dict) and step.get("status") not in (
                "success",
                "skipped",
                None,
            ):
                fix = str(step.get("fix") or "").split(". The ")[0]
                return f"{step.get('step')} — {fix or step.get('status')}"
    return _last_line(out, err)


def _delegation_fix(out: str, err: str, *, on: bool) -> str:
    """Why `onex delegation on|off --json` failed, in the command's own words:
    its top-level fix, and for off the claude-hooks step's, which names a
    refused hook removal (review 2026-11-15: the first failing setup step's fix
    sent the user to `onex setup`, which the TUI does not offer)."""
    if _SETUP_LOCK in out or _SETUP_LOCK in err:
        return _say("setup_busy")
    document = _last_json_object(out)
    if document is None:
        return _stopped_by(out, err)
    steps = document.get("observations")
    hooks = next(
        (
            step
            for step in (steps if isinstance(steps, list) else ())
            if isinstance(step, dict)
            and step.get("step") == "claude-hooks"
            and step.get("status") not in ("success", None)
            and step.get("fix")
        ),
        None,
    )
    if not on and hooks is not None:
        return str(hooks["fix"])
    fix = document.get("fix")
    return str(fix) if fix else _stopped_by(out, err)


#: Both setup lock refusals (setup run, and setup or plugin change) contain it.
_SETUP_LOCK = "holds the setup lock"

# The CLI's failure text leads with how delegation routed ("returned_to_caller:
# quality_veto; no model you chose finished the task; ..."), and the cause comes
# last, often after the first 160 characters. These drop that framing.
_LAST_ERROR = "last error:"
_PINNED = re.compile(r"^pinned_backend_failed: .*? does not move to another backend: ")
_TAG = re.compile(r"^(?:[A-Z][A-Z_]+|[a-z]+(?:_[a-z]+)+): ")
_TICKET_NOTE = re.compile(r"\s*\(OMN-\d+[^)]*\)?\s*$")
_FRAMING = ("returned_to_caller", "no model you chose", "do the task in this session")


def plain_reason(raw: str, limit: int) -> str:
    """The cause in a CLI failure, without its routing framing, cut at a word."""
    text = " ".join(raw.split())
    if _LAST_ERROR in text:
        text = text.rsplit(_LAST_ERROR, 1)[1].strip()
    text = _PINNED.sub("", text.removeprefix("Error: "))
    kept: list[str] = []
    for clause in text.split("; "):
        while _TAG.match(clause):
            clause = _TAG.sub("", clause, count=1)
        clause = clause.rsplit(" -- ", 1)[-1]
        if " " not in clause.strip() or any(f in clause for f in _FRAMING):
            continue
        kept.append(clause.strip())
    text = _TICKET_NOTE.sub("", "; ".join(kept))
    if text.count("(") > text.count(")"):
        text += ")"
    cut = text.endswith("...")
    if cut:
        text = text[:-3].rsplit(" ", 1)[0]
    if len(text) > limit:
        text, cut = text[: limit - 1].rsplit(" ", 1)[0], True
    return text + "…" if cut and text else text


def _tested_line(provider: str, result: Mapping[str, object]) -> Text:
    """One model's test result as one line: ready, busy, or stored but failing."""
    glyphs = tui_design().glyphs
    model = str(result.get("model"))
    status = result.get("status")
    if status == "passed":
        return Text(
            f"{glyphs.done} " + _say("model_ready", model=model, provider=provider),
            style=_style("success"),
        )
    short = plain_reason(str(result.get("reason") or ""), tui_design().reason_chars)
    short = short or str(status)
    if status == "throttled":
        return Text(
            f"{glyphs.unchanged} " + _say("model_busy", model=model, reason=short),
            style=_style("warning"),
        )
    return Text(
        f"{glyphs.failed} " + _say("model_test_failed", model=model, reason=short),
        style=_style("error"),
    )


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
    return listed_catalog_entry(provider, model, index, provider_label=_label(provider))


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


class _TabRule(Underline):
    """The heavy rule under the open tab only, as wide as its label."""

    def render(self) -> RenderResult:
        start, end = self._highlight_range
        start = max(0, round(start))
        rule = _say("tab_rule") * max(0, round(end) - start)
        return Text(" " * start + rule, style=_style("primary", "bold"), no_wrap=True)


class _TabBar(Tabs):
    """The tab strip: plain labels, three spaces apart, a rule under the open one."""

    DEFAULT_CSS = """
    _TabBar Tab { padding: 0; margin: 0 3 0 0; color: $text-muted; }
    _TabBar Tab.-active { color: $text; text-style: bold; background: transparent; }
    _TabBar Tab:hover { background: transparent; }
    _TabBar Underline { width: 1fr; }
    """

    def compose(self) -> ComposeResult:
        with Container(id="tabs-scroll"), Vertical(id="tabs-list-bar"):
            with Horizontal(id="tabs-list"):
                yield from self._tabs
            yield _TabRule()


def _tab_strip(current: str) -> Tabs:
    """The same strip on all four tabs, the current one underlined."""
    strip = _TabBar(
        *(Tab(spec.label, id=f"tab-{spec.id}") for spec in tui_design().tabs),
        active=f"tab-{current}",
        id="tabs",
    )
    strip.can_focus = False
    return strip


def _tab_hint(name: str) -> Text:
    """A tab's one footer line: each part's first word is its key, in bold."""
    text = Text(no_wrap=True, overflow="ellipsis")
    separator = _say("hint_separator")
    for position, part in enumerate(_say(name).split(separator)):
        key, _, action = part.partition(" ")
        if position:
            text.append(separator, style=_style("muted"))
        text.append(key, style=_style("foreground", "bold"))
        if action:
            text.append(f" {action}", style=_style("muted"))
    return text


def _summary(selection: ModelTuiSelection) -> Static:
    """The one line under the tabs, as the widget each tab shows it in."""
    return Static(_summary_text(selection), id="summary", classes="summary")


def _summary_text(selection: ModelTuiSelection) -> Text:
    """The one line under the tabs: how many models, on how many servers, the first."""
    servers = len({e.server_url for e in selection.entries if e.server_url})
    first = (
        _say(
            "summary_first",
            model=_on_server(selection.first[1], selection.first_url),
        )
        if selection.first
        else _say("summary_auto")
    )
    line = _say(
        "summary" if servers else "summary_no_servers",
        dot=tui_design().model_table.status_pill,
        models=_count(len(selection.entries), "model"),
        servers=_count(servers, "server"),
        first=first,
    )
    return Text(line, style=_style("secondary", "bold"))


def _base(url: str) -> str:
    """A server URL as the CLI stores it: no trailing ``/`` or ``/v1``."""
    return url.strip().rstrip("/").removesuffix("/v1")


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

    #: The id of the line this tab shows its notes and outcomes in.
    NOTE: ClassVar[str] = ""
    _busy: bool = False

    def _leave(self, target: str) -> None:
        """Swap this tab for another; this one stays up while the next is read."""
        app = self.app
        if not isinstance(app, OnexTui):
            return
        if self._busy:  # the running action's outcome shows here first
            self._note(_say("still_working"), "warning")
            return
        label = next(
            (t.label.strip() for t in tui_design().tabs if t.id == target), target
        )
        self._note(_say("tab_loading", tab=label))
        app.run_worker(app.switch_tab(target), exclusive=True, group="tabs")

    def _close(self) -> None:
        """Esc or Cancel closes the tab, except while its action is running."""
        if self._busy:
            self._note(_say("still_working"), "warning")
            return
        self.dismiss(cast(_ResultT, None))

    #: The words key of this tab's plain problem sentence (tui_design.yaml).
    PROBLEM: ClassVar[str] = "problem_config"

    def _calm(self, text: Text | str) -> Text | str:
        """A line that carries a raw cause (an errno, a path, an exception or an
        environment name) goes to the log; the screen says what to do instead."""
        plain = text.plain if isinstance(text, Text) else text
        if re.search(tui_design().words["raw_text_pattern"], plain):
            _LOG.warning("tab problem: %s", plain)
            return Text(_say(self.PROBLEM), style=_style("warning"))
        return text

    def _problem(self, error: BaseException) -> None:
        """An action or a read raised: log it, tell the user in plain words."""
        _LOG.warning("tab problem: %r", error)
        with contextlib.suppress(NoMatches):
            self._note(Text(_say(self.PROBLEM), style=_style("warning")))

    def _note(self, text: Text | str, style: str = "muted") -> None:
        text = self._calm(text)
        line = text if isinstance(text, Text) else Text(text, style=_style(style))
        self.query_one(self.NOTE, Static).update(line)

    def _run(self, action: Callable[[OnexTui], Awaitable[Text | str | None]]) -> None:
        """Run an action while the tab stays open, then show how it went and
        what is set up now (OMN-23413: only Esc closes a tab)."""
        app = self.app
        if self._busy or not isinstance(app, OnexTui):
            return
        self._busy = True
        self._note(_say("working"))

        async def go() -> None:
            try:
                outcome = await action(app)
                self._selection = await app.load_selection()
                self._show_selection()
                if outcome is not None:
                    self._note(outcome)
            except Exception as error:
                self._problem(error)
            finally:
                self._busy = False

        self.run_worker(go(), exclusive=True, group="tab-action")

    _selection: ModelTuiSelection

    def refresh_selection(self) -> None:
        """Read the setup again in the background and show it (a tab opened
        from the setup as last read)."""
        app = self.app
        if not isinstance(app, OnexTui):
            return

        async def go() -> None:
            try:
                self._selection = await app.load_selection()
                self._show_selection()
            except Exception as error:
                self._problem(error)

        self.run_worker(go(), exclusive=True, group="tab-refresh")

    def _show_selection(self) -> None:
        """Show ``self._selection`` again after an action changed it."""
        self.query_one("#summary", Static).update(_summary_text(self._selection))


class SetupScreen(_TabbedScreen[None]):
    """Provider, then its key or URL, then any number of its models; repeat per provider."""

    TAB = "config"
    NOTE = "#note"

    CSS = """
    SetupScreen { align: center middle; }
    #form { width: 92%; max-width: 160; height: 92%; border: round $primary; padding: 0 2; background: $panel; }
    #form .summary { display: none; }
    #cfg-head { height: 1; }
    #cfg-title { width: 1fr; text-style: bold; color: $primary; }
    #cfg-step { width: auto; color: $text-muted; }
    #cfg-row { height: auto; }
    #provider { width: 24; height: auto; border: round $border; border-title-color: $text-muted; padding: 0 1; background: transparent; }
    #keybox { width: 1fr; height: 3; border: round $border; border-title-color: $text-muted; margin-left: 2; }
    #keybox Input { width: 1fr; height: 1; border: none; padding: 0 1; background: transparent; }
    #keymark { width: auto; padding: 0 1; }
    #found { height: 1fr; min-height: 3; display: none; }
    #note { color: $text-muted; height: auto; }
    #basket { color: $success; height: auto; }
    #keys { height: 1; }
    #buttons { height: 1; align-horizontal: right; }
    #buttons Button { height: 1; min-width: 8; border: none; margin: 0 0 0 2; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Back"),
        Binding("ctrl+s", "save", _say("button_save"), priority=True),
        Binding("ctrl+g", "guided_setup", _say("button_guided_setup"), priority=True),
    ]

    def __init__(
        self,
        runner: ProtocolTuiRunner,
        list_models: ListModels | None = None,
        load_index: LoadIndex | None = None,
        read_server: ReadServer | None = None,
        selection: ModelTuiSelection | None = None,
    ) -> None:
        super().__init__()
        self._runner = runner
        self._selection = selection or ModelTuiSelection()
        self._list_models: ListModels = list_models or list_and_remember_models
        self._load: LoadIndex = load_index or _default_index
        self._read_server: ReadServer = read_server or _default_read_server
        self._basket: dict[str, ModelTuiSetupChoice] = {}
        self._find_timer: Timer | None = None
        self._loading = False

    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield _tab_strip(self.TAB)
            yield _summary(self._selection)
            with Horizontal(id="cfg-head"):
                yield Static(_say("add_title"), id="cfg-title")
                yield Static(self._step_text(1), id="cfg-step")
            choices = _picker()
            first = choices[0]
            with Horizontal(id="cfg-row"):
                yield Choice(
                    [(e.label, e.provider) for e in choices],
                    first.provider,
                    id="provider",
                )
                with Horizontal(id="keybox"):
                    yield Input(
                        placeholder=_say("key_placeholder"), password=True, id="key"
                    )
                    yield Input(
                        value=self._stored_url(first.provider) or "",
                        placeholder=_say("url_placeholder"),
                        id="url",
                    )
                    yield Static("", id="keymark")
            yield Static("", id="note")
            yield ModelTable(
                selectable=True,
                provider_labels={e.provider: e.label for e in choices},
                id="found",
                layout="found",
            )
            yield Static(self._selected_line(), id="basket")
            with Horizontal(id="buttons"):
                yield Button(_say("button_guided_setup"), id="guided-setup")
                yield Button(_say("button_add"), id="add")
                yield Button(_say("button_save"), variant="primary", id="save")
                yield Button(_say("button_cancel"), id="cancel")
            yield Static(_tab_hint("tab_hint_config"), id="keys")

    def _step_text(self, step: int) -> Text:
        return Text(_say("step_of", step=step), style=_style("muted"))

    def _step(self) -> int:
        """1 choosing a provider and key, 2 models found, 3 something chosen."""
        table = self.query_one("#found", ModelTable)
        if self._basket or table.ticked:
            return 3
        return 2 if table.entries else 1

    def _show_step(self) -> None:
        self.query_one("#cfg-step", Static).update(self._step_text(self._step()))

    def _selected_line(self) -> str:
        """The models ticked or added, each with the model mark; or none yet."""
        chosen: list[str] = []
        for pick in self._basket.values():
            chosen.extend(pick.models)
        if self.is_mounted:
            chosen.extend(
                e.model_id for e in self.query_one("#found", ModelTable).ticked
            )
        names = list(dict.fromkeys(chosen))
        if not names:
            return _say("selected_none")
        mark = _say("model_glyph")
        return _say("selected", models="  ".join(f"{mark} {n}" for n in names))

    def on_mount(self) -> None:
        self.query_one("#found").display = False
        self.query_one("#provider").border_title = _say("provider_box")
        self._show_fields()
        self.query_one("#note", Static).update(_hint(self._provider()))
        self._schedule_find()

    def _provider(self) -> str:
        return self.query_one("#provider", Choice).value

    def _stored_url(self, provider: str) -> str | None:
        """The server the user already runs ``provider``'s models on, if any."""
        return next(
            (
                e.server_url
                for e in self._selection.entries
                if e.provider == provider and e.server_url
            ),
            None,
        )

    def _current_line(self) -> str:
        """What is set up already, or that nothing is, before anything is ticked."""
        if not self._selection.entries:
            return _say("basket_empty")
        named = _say(
            "basket_current",
            models=", ".join(
                _on_server(e.model_id, e.server_url) for e in self._selection.entries
            ),
        )
        if len(named) <= tui_design().basket_names_max_chars:
            return named
        per_provider = Counter(e.provider for e in self._selection.entries)
        total = len(self._selection.entries)
        return _say(
            "basket_counted",
            count=f"{total} model{'' if total == 1 else 's'}",
            providers=" · ".join(f"{_label(p)} {n}" for p, n in per_provider.items()),
        )

    def _as_stored(self, entry: ModelCatalogEntry, url: str) -> ModelCatalogEntry:
        """A found model already set up shows its stored status, not "listed"."""
        stored = next(
            (
                e
                for e in self._selection.entries
                if e.provider == entry.provider
                and e.model_id == entry.model_id
                and (e.server_url is None or e.server_url == _base(url))
            ),
            None,
        )
        if stored is None:
            return entry
        return entry.model_copy(
            update={"status": stored.status, "status_reason": stored.status_reason}
        )

    def _value(self, widget_id: str) -> str:
        return self.query_one(f"#{widget_id}", Input).value.strip()

    def _show_fields(self) -> None:
        hosted = _hosted(self._provider())
        url = self.query_one("#url", Input)
        # A default the user has not edited follows the provider; a typed URL stays.
        # An empty field offers the server already set up; a typed URL stays.
        if not hosted and not url.value.strip():
            url.value = self._stored_url(self._provider()) or ""
        self.query_one("#key").display = hosted
        self.query_one("#url").display = not hosted
        self.query_one("#keybox").border_title = _say(
            "key_box" if hosted else "url_box"
        )
        self._mark_key(None)

    def _mark_key(self, word: str | None) -> None:
        """The mark beside the key: valid or not accepted, from the list read with it."""
        mark = self.query_one("#keymark", Static)
        if word is None:
            mark.update("")
            return
        colour = "success" if word in ("key_valid", "url_valid") else "warning"
        mark.update(Text(_say(word), style=_style(colour)))

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
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
        except NoMatches:  # the user left this tab while the list was loading
            return
        except Exception as error:  # a listing that raised: say so, calmly
            self._problem(error)
            self._mark_key(None)
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
            accepted = listing.outcome == "listed"
            rejected = listing.outcome == "rejected"
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
            accepted, rejected = bool(found), False
        if self._source() != wanted:  # the key, URL or provider changed meanwhile
            return
        entries = [self._as_stored(e, wanted[1]) for e in entries]
        self._show_rows(entries)
        if entries:
            self.query_one("#found", ModelTable).focus_rows()
        hosted = _hosted(provider)
        if accepted:
            self._mark_key("key_valid" if hosted else "url_valid")
        elif rejected:
            self._mark_key("key_rejected")
        if not entries:
            say = message or _say("none_found")
        elif len(entries) == 1:
            say = _say("models_found_one")
        else:
            say = _say("models_found", count=len(entries))
        note.update(say)
        self._show_step()

    def _load_index(self) -> ModelsDevIndex:
        return _safe_index(self._load)

    def _show_rows(self, entries: list[ModelCatalogEntry]) -> None:
        """New results: forget earlier ticks; the table shows only a read list."""
        table = self.query_one("#found", ModelTable)
        table.set_ticked(())
        table.set_entries(entries)
        table.display = bool(entries)
        self._show_step()
        self.query_one("#basket", Static).update(self._basket_line())

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in ("key", "url"):
            self._mark_key(None)
            self._schedule_find()

    @on(ModelTable.Ticked)
    def _ticked(self) -> None:
        self.query_one("#basket", Static).update(self._basket_line())
        self._show_step()

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
        self.query_one("#basket", Static).update(self._basket_line())
        self._show_step()
        return True

    def _basket_line(self) -> str:
        """What is added and not yet set up, or what is set up when nothing is."""
        return self._selected_line()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        note = self.query_one("#note", Static)
        if event.button.id == "cancel":
            self._close()
        elif event.button.id == "add":
            if self._loading:
                note.update(_say("still_loading"))
            elif self._add_current():
                note.update(_say("added"))
                self.app.notify(
                    _say("added"),
                    title=_say("toast_added"),
                    timeout=tui_design().toast_seconds,
                )
            else:
                note.update(_say("tick_first"))
        elif event.button.id == "save":
            self.action_save()
        elif event.button.id == "guided-setup":
            self.action_guided_setup()

    def action_guided_setup(self) -> None:
        if self._basket or self.query_one("#found", ModelTable).ticked:
            self._note(_say("guided_setup_pending"), "warning")
            return
        self._run(OnexTui.guided_setup)

    def action_save(self) -> None:
        """Set up what is ticked or added, here, and stay; refuse with nothing chosen."""
        if self._busy:
            return
        self._add_current()
        if not self._basket:
            warning = tui_design().delegation.nothing_ticked
            self.query_one("#note", Static).update(
                Text(warning, style=_style("warning"))
            )
            self.app.notify(warning, severity="warning")
            return
        choices = tuple(self._basket.values())

        async def save(app: OnexTui) -> Text | str | None:
            line = await app.apply_setup(choices, progress=self._note)
            if app.setup_ok:  # a failed save keeps the choices for a retry
                self._basket = {}
            return line

        self._run(save)

    def _show_selection(self) -> None:
        super()._show_selection()
        self.query_one("#found", ModelTable).set_ticked(())
        self.query_one("#basket", Static).update(self._basket_line())

    def action_cancel(self) -> None:
        self._close()


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
    return ModelTuiSelection(entries=tuple(entries), first=first, first_url=first_url)


class ModelTuiDelegationChoice(BaseModel):
    """Claude Code delegation on or off, and the onex commands that do it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    on: bool
    commands: tuple[tuple[str, ...], ...]
    #: What a successful run says; ``None`` is the plain on or off sentence.
    done: str | None = None


class _Radio(RadioButton):
    """A radio button drawn as one marker, filled or hollow, from the design."""

    DEFAULT_CSS = """
    _Radio { padding: 0; background: transparent; }
    _Radio > .toggle--button { background: transparent; color: $text-muted; }
    _Radio.-on > .toggle--button { color: $primary; }
    _Radio.-selected { background: $boost; }
    """

    @property
    def _button(self) -> Content:
        glyph = _say("radio_on" if self.value else "radio_off")
        return Content.assemble((glyph, self.get_visual_style("toggle--button")))


class Choice(RadioSet):
    """A choice with every option shown and one marked; the TUI has no dropdown.

    Operator 2026-11-13, on a dropdown that showed the chosen mode above a list
    holding it again: "why two?", then "in tui no dropdown at any places,
    instead use radio buttons for visibility". ``value`` is the marked option's
    id, as a Select's was.
    """

    def __init__(
        self, options: list[tuple[str, str]], value: str, *, id: str | None = None
    ) -> None:
        self._ids = [option_id for _, option_id in options]
        #: The marked id. A RadioSet never lets its pressed button be turned off
        #: directly (it turns it back on), so the id is kept here rather than
        #: read from buttons that are mid-change.
        self._value = value if value in self._ids else self._ids[0]
        super().__init__(
            *(
                _Radio(label, value=option_id == self._value, name=option_id)
                for label, option_id in options
            ),
            id=id,
        )

    @property
    def value(self) -> str:
        return self._value

    @value.setter
    def value(self, wanted: str) -> None:
        if wanted not in self._ids:
            raise ValueError(f"{wanted!r} is not one of {self._ids}")
        self._value = wanted
        for button in self.query(RadioButton):
            if button.name == wanted:
                button.value = True  # the RadioSet turns the old one off

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        # A click or a key marked another option; the event goes on to the screen.
        if event.pressed.name:
            self._value = event.pressed.name


class DelegationScreen(_TabbedScreen[None]):
    """Your selected models and one switch per delegation hook; nothing is typed
    (OMN-23413 AC25; operator 2026-11-15: "build separate switches")."""

    CSS = """
    DelegationScreen { align: center middle; }
    #deleg { width: 92%; max-width: 160; height: 92%; border: round $primary; padding: 0 2; background: $panel; }
    #deleg .summary { display: none; }
    #dhead { height: 1; }
    #dtitle { width: 1fr; text-style: bold; color: $primary; }
    #on-state { width: auto; text-style: bold; }
    #hooks-title { color: $text-muted; height: 1; }
    #dturn { width: auto; min-width: 12; height: 1; border: none; margin: 1 0; }
    #dswitches { display: none; height: 0; }
    .hook { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #dorder-title { color: $text-muted; height: 1; margin-top: 1; }
    #selected { height: 1fr; }
    #selected DataTable { height: auto; max-height: 100%; }
    #claude { height: auto; }
    #dfoot { height: auto; }
    #dnote { color: $text-muted; height: auto; }
    #dkeys { height: 1; }
    #dbuttons { height: 1; align-horizontal: right; }
    #dbuttons Button { height: 1; min-width: 8; border: none; margin: 0 0 0 2; }
    """
    TAB = "delegation"
    NOTE = "#dnote"
    PROBLEM = "problem_delegation"
    BINDINGS = [
        Binding("escape", "cancel", _say("button_cancel")),
        Binding("ctrl+s", "save", _say("button_save"), priority=True),
        Binding("space", "flip", "Switch both", show=False),
    ]

    def __init__(
        self,
        selection: ModelTuiSelection,
        delegation: DelegationState | None = None,
        hooks: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._selection = selection
        #: The state as last read: ``None`` until a read lands, "unknown" when
        #: it failed; neither is drawn as OFF (review 2026-11-15).
        self._saved = delegation
        #: Drawn as it is (OMN-23413, 2026-11-13: hook delegation that was on
        #: once showed off).
        self._enabled = delegation == "hook"
        #: Each hook's installed state, as status read it (OMN-23413).
        self._hooks = dict(hooks or {})
        #: Claude Code's state once the check answers; "checking" until then.
        self._claude_state = "checking"
        self._claude_fix = ""

    def compose(self) -> ComposeResult:
        words = tui_design().delegation
        with Vertical(id="deleg"):
            yield _tab_strip(self.TAB)
            yield _summary(self._selection)
            with Horizontal(id="dhead"):
                yield Static(Text(words.title), id="dtitle")
                yield Static(self._state_text(), id="on-state")
            yield Static(
                Text(_say("delegation_about"), style=_style("muted")), id="hooks-title"
            )
            yield Button(
                _say("turn_off" if any(self._saved_boxes().values()) else "turn_on"),
                id="dturn",
            )
            # The switches hold the pending state; the button and Space move them.
            with Horizontal(id="dswitches"):
                saved = self._saved_boxes()
                for hook in words.hooks:
                    with Horizontal(classes="hook-switch"):
                        yield Switch(value=saved[hook.key], id=f"use-{hook.key}")
            for hook in words.hooks:
                yield Static(
                    self._hook_text(hook.key), id=f"hook-{hook.key}", classes="hook"
                )
            # OMN-23413: is Claude Code there? Read from setup's own check.
            yield Static(
                Text(_say("claude_checking"), style=_style("muted")), id="claude"
            )
            yield Static(
                Text(_say("order_title"), style=_style("muted")), id="dorder-title"
            )
            yield ModelTable(
                selectable=False,
                provider_labels={e.provider: e.label for e in _picker()},
                id="selected",
                layout="order",
            )
            with Vertical(id="dfoot"):
                yield Static("", id="dnote")
                with Horizontal(id="dbuttons"):
                    yield Button(_say("button_save"), variant="primary", id="dsave")
                    yield Button(_say("button_cancel"), id="dcancel")
                yield Static(_tab_hint("tab_hint_delegation"), id="dkeys")

    def on_mount(self) -> None:
        self.run_worker(self._check_claude(), group="claude", exclusive=True)
        table = self.query_one("#selected", ModelTable)
        table.set_entries(self._selection.entries)
        # The keyboard goes to the list without scrolling the switch away.
        table.query_one("#mt-rows", DataTable).focus(scroll_visible=False)
        words = tui_design().delegation
        note = self.query_one("#dnote", Static)
        if not self._selection.entries:
            note.update(Text(words.no_models, style=_style("warning")))

    async def _check_claude(self) -> None:
        """Fill the Claude Code line: shown only when Claude Code is not ready."""
        state, fix = await cast("OnexTui", self.app).claude_code()
        self._claude_state = state
        self._claude_fix = fix
        line, colour = {
            "ready": (_say("claude_ready"), "success"),
            "installed": (_say("claude_installed"), "info"),
            "signed_out": (_say("claude_signed_out"), "warning"),
            "not_installed": (_say("claude_missing"), "error"),
        }.get(state, (_say("claude_unknown"), "muted"))
        # While Claude Code is only installed, Save adds the plugin (the line says so);
        # the CLI's repair sentence is a developer command, so it goes to the log.
        # Every other state keeps the CLI's plain next step (OMN-26617).
        if fix and state == "installed":
            _LOG.info("claude code hint hidden: %s", fix)
            fix = ""
        rendered = line if not fix else f"{line}\n{fix}"
        shown = self._calm(Text(rendered, style=_style(colour)))
        widget = self.query_one("#claude", Static)
        widget.update(shown)
        widget.display = state != "ready"

    def action_flip(self) -> None:
        """Space switches both: on when any is off, otherwise off."""
        self.set_all(not all(self._boxes().values()))

    def set_all(self, value: bool) -> None:
        for switch in self.query(".hook-switch Switch").results(Switch):
            switch.value = value

    def any_on(self) -> bool:
        """Whether a switch, as it stands, delegates through its hook."""
        return any(self._boxes().values())

    def _state_text(self) -> Text:
        """ON (a switch delegates) or OFF in words, and "press Save to apply"
        while the switches differ from the saved state (OMN-23413, operator
        2026-11-14). A state not read yet, or whose read failed, is said as
        such, never as OFF (review 2026-11-15)."""
        words = tui_design().delegation
        boxes = self._boxes()
        on = any(boxes.values())
        moved = boxes != self._saved_boxes()
        if not moved and self._saved in (None, "unknown"):
            reading = self._saved is None
            return Text(
                f"{words.glyph_unknown} "
                + (words.state_reading if reading else words.state_unread),
                style=_style("muted" if reading else "warning", "bold"),
            )
        parts: list[tuple[str, str]] = [
            (
                f"{words.glyph_on} {words.state_on}"
                if on
                else f"{words.glyph_off} {words.state_off}",
                _style("success" if on else "muted", "bold"),
            )
        ]
        if moved:
            parts.append((f"  · {words.state_pending}", _style("warning")))
        return Text.assemble(*parts)

    def _saved_boxes(self) -> dict[str, bool]:
        """Which hooks delegate, as status read them: none while delegation is
        off, and not a paused one."""
        return {
            hook.key: self._enabled and self._hooks.get(hook.key) != "paused"
            for hook in tui_design().delegation.hooks
        }

    def _boxes(self) -> dict[str, bool]:
        """Each hook's switch as it stands, or as composed when not drawn yet."""
        drawn = {
            switch.id: switch.value
            for switch in self.query(".hook-switch Switch").results(Switch)
        }
        saved = self._saved_boxes()
        return {key: drawn.get(f"use-{key}", saved[key]) for key in saved}

    def _hook_text(self, key: str) -> Text:
        """The hook line: its label, then whether Claude Code has the hook."""
        state = self._hooks.get(key)
        unread = (
            "hook_state_unread" if self._saved == "unknown" else "hook_state_unknown"
        )
        word, colour = {
            "installed": ("hook_state_installed", "success"),
            "missing": ("hook_state_missing", "warning"),
            "paused": ("hook_state_paused", "warning"),
        }.get(state or "", (unread, "muted"))
        return Text.assemble(
            (f"{_say('hook_label')}   ", _style("primary", "bold")),
            (_say(word), _style(colour)),
        )

    def show_state(
        self, delegation: DelegationState | None, hooks: dict[str, str]
    ) -> None:
        """Redraw from status read again: after a Save, when the start-up read
        lands, after a palette command (OMN-23413, review 2026-11-15). A switch
        the user has not moved moves to the state read."""
        untouched = self._boxes() == self._saved_boxes()
        self._saved = delegation
        self._enabled = delegation == "hook"
        self._hooks = dict(hooks)
        if not list(self.query("#on-state")):  # not composed: compose draws it
            return
        for hook in tui_design().delegation.hooks:
            self.query_one(f"#hook-{hook.key}", Static).update(
                self._hook_text(hook.key)
            )
        if untouched:
            for key, value in self._saved_boxes().items():
                self.query_one(f"#use-{key}", Switch).value = value
        self.query_one("#on-state", Static).update(self._state_text())
        self._show_turn()

    def on_switch_changed(self, _event: Switch.Changed) -> None:
        self.query_one("#on-state", Static).update(self._state_text())
        self._show_turn()

    def _show_turn(self) -> None:
        """The button names what pressing it does: switch off while on, else on."""
        self.query_one("#dturn", Button).label = _say(
            "turn_off" if self.any_on() else "turn_on"
        )

    def action_save(self) -> None:
        """Switch here and stay; the outcome shows on this tab."""
        if self.any_on() and not self._selection.entries:
            self._note(tui_design().delegation.no_models, "warning")
            return
        self._run(self._save)

    async def _save(self, app: OnexTui) -> Text | str | None:
        """Check Claude Code again, then run the switch's commands, or say why
        nothing was sent. Nothing is sent from a state nobody has read, and an
        untouched switch never sends off over a failed read (review 2026-11-15)."""
        words = tui_design().delegation
        boxes = self._boxes()
        on = any(boxes.values())  # both off is delegation off: the hooks go
        touched = boxes != self._saved_boxes()
        unread = self._saved not in ("off", "hook")
        if on or unread:  # the tab may be minutes old: ask again, not the cache
            await self._check_claude()
        if self._saved is None:
            return Text(words.save_reading, style=_style("warning"))
        if unread and not touched:
            said = (
                words.save_unread if self._saved == "unknown" else words.save_just_read
            )
            return Text(said, style=_style("warning"))
        if on and self._claude_state == "not_installed":  # the user installs it
            line = str(self.query_one("#claude", Static).render())
            return Text(line, style=_style("warning"))
        if not on:
            return await app.apply_delegation(
                ModelTuiDelegationChoice(on=False, commands=words.disable)
            )
        # One hook, one switch (OMN-23413 Amendment 14a): on is the hook on.
        return await app.apply_delegation(
            ModelTuiDelegationChoice(on=True, commands=words.enable)
        )

    def _show_selection(self) -> None:
        super()._show_selection()
        self.query_one("#selected", ModelTable).set_entries(self._selection.entries)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dcancel":
            self._close()
        elif event.button.id == "dturn":
            self.action_flip()
        else:
            self.action_save()

    def action_cancel(self) -> None:
        self._close()


class ModelPickScreen(_TabbedScreen[None]):
    """Auto, or the model every task tries first, from the one model table."""

    TAB = "model"
    NOTE = "#pnote"
    PROBLEM = "problem_model"

    CSS = """
    ModelPickScreen { align: center middle; }
    #pick { width: 92%; max-width: 160; height: 92%; border: round $primary; padding: 0 2; background: $panel; }
    #pick .summary { display: none; }
    #pnow { height: 1; text-style: bold; }
    #models { height: 1fr; }
    #pnote { color: $text-muted; height: auto; }
    #pkeys { height: 1; }
    #auto { width: auto; height: 1; min-width: 8; border: none; margin: 0; }
    """
    BINDINGS = [
        Binding("escape", "cancel", _say("button_cancel")),
        Binding("a", "auto", "Auto", show=False),
        Binding("r,delete", "remove", "Remove", show=False),
        Binding("d", "details", "Details", show=False),
    ]

    def __init__(self, selection: ModelTuiSelection) -> None:
        super().__init__()
        self._selection = selection
        #: The row R was pressed on once; a second R on it removes the model.
        self._removing: str | None = None

    def compose(self) -> ComposeResult:
        words = tui_design().model_table
        with Vertical(id="pick"):
            yield _tab_strip(self.TAB)
            yield _summary(self._selection)
            yield Static(self._now(), id="pnow")
            yield ModelTable(
                selectable=False,
                provider_labels={e.provider: e.label for e in _picker()},
                id="models",
                layout="pick",
            )
            yield Static("", id="pnote")
            yield Button(words.auto_label + "  (A)", id="auto")
            yield Static(_tab_hint("tab_hint_model"), id="pkeys")

    def _first_key(self) -> str | None:
        """The table key of the model tried first, when one is chosen."""
        first = self._selection.first
        if first is None:
            return None
        key = f"{first[0]}\t{first[1]}"
        return (
            f"{key}\t{self._selection.first_url}" if self._selection.first_url else key
        )

    def on_mount(self) -> None:
        table = self.query_one("#models", ModelTable)
        table.set_first(self._first_key())
        table.set_entries(self._selection.entries)
        table.query_one("DataTable").focus()
        if not self._selection.entries:
            self._note(_say("nothing_set_up"), "warning")

    def _now(self) -> Text:
        first = self._selection.first
        mark = _say("model_glyph")
        if first is None:
            line = _say("tried_first_auto", mark=mark)
        else:
            line = _say(
                "tried_first",
                mark=mark,
                model=_on_server(first[1], self._selection.first_url),
            )
        return Text(line, style=_style("primary", "bold"))

    def _show_selection(self) -> None:
        super()._show_selection()
        table = self.query_one("#models", ModelTable)
        table.set_first(self._first_key())
        table.set_entries(self._selection.entries)
        self.query_one("#pnow", Static).update(self._now())

    @on(ModelTable.Chosen)
    def _chosen(self, event: ModelTable.Chosen) -> None:
        entry = event.entry
        if entry.status in ("failed", "unavailable"):
            reason = (
                entry.status_reason
                or tui_design().model_table.status[entry.status].text
            )
            self._note(
                Text(
                    _say("cannot_be_first", model=entry.model_id, reason=reason),
                    style=_style("warning"),
                )
            )
            return
        picked = (entry.provider, entry.model_id, entry.server_url or "")
        self._run(lambda app: app.apply_model(picked))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "auto":
            self.action_auto()

    def action_auto(self) -> None:
        self._run(lambda app: app.apply_model(("", "", "")))

    def action_details(self) -> None:
        self.query_one("#models", ModelTable).action_details()

    @on(DataTable.RowHighlighted)
    def _moved(self) -> None:
        """Moving to another row forgets a pending remove."""
        if self._removing is not None:
            self._removing = None
            self.query_one("#pnote", Static).update("")

    def action_remove(self) -> None:
        """R asks; R again on the same row removes that model (from that server)."""
        entry = self.query_one("#models", ModelTable).current()
        if entry is None:
            return
        key = entry_key(entry)
        named = _on_server(entry.model_id, entry.server_url)
        if self._removing != key:
            self._removing = key
            self.query_one("#pnote", Static).update(
                Text(
                    _say(
                        "remove_confirm", model=named, provider=_label(entry.provider)
                    ),
                    style=_style("warning", "bold"),
                )
            )
            return
        self._removing = None
        self._run(lambda app: app.remove_model(entry))

    def action_cancel(self) -> None:
        self._close()


class DashboardScreen(_TabbedScreen[None]):
    """The local dashboard: its real state, and Start, Open and Stop (OMN-23413)."""

    TAB = "dashboard"
    NOTE = "#bnote"
    PROBLEM = "problem_dashboard"

    CSS = """
    DashboardScreen { align: center middle; }
    #board { width: 92%; max-width: 160; height: 92%; border: round $primary; padding: 0 2; background: $panel; }
    #board .summary { display: none; }
    #bhead { height: 1; }
    #btitle { width: 1fr; text-style: bold; color: $primary; }
    #bstate { width: auto; text-style: bold; }
    #baddr { height: 1; color: $accent; }
    #bbuttons { height: 1; margin: 1 0; }
    #bbuttons Button { height: 1; min-width: 8; width: auto; border: none; margin: 0 2 0 0; }
    #bnote { height: auto; }
    #bkeys { height: 1; dock: bottom; }
    """
    BINDINGS = [
        Binding("escape", "cancel", _say("button_cancel")),
        Binding("s", "start_or_stop", "Start or stop", show=False),
        Binding("enter", "open", _say("button_open"), show=False),
        Binding("x", "stop", _say("button_stop"), show=False),
    ]

    def __init__(self, selection: ModelTuiSelection) -> None:
        super().__init__()
        self._selection = selection
        #: Whether the dashboard runs, once its state was read; ``None`` before.
        self._dash_running: bool | None = None

    def compose(self) -> ComposeResult:
        words = tui_design().dashboard
        with Vertical(id="board"):
            yield _tab_strip(self.TAB)
            yield _summary(self._selection)
            with Horizontal(id="bhead"):
                yield Static(Text(words.title), id="btitle")
                yield Static(self._state_line(None), id="bstate")
            yield Static("", id="baddr")
            with Horizontal(id="bbuttons"):
                # The keys are S, Enter and X; a focused button would take Enter
                # for itself. The buttons stay clickable.
                for label, button_id in (
                    (_say("button_start"), "bstart"),
                    (words.button_open, "bopen"),
                    (_say("button_stop"), "bstop"),
                ):
                    button = Button(
                        label,
                        variant="primary" if button_id == "bstart" else "default",
                        id=button_id,
                    )
                    button.can_focus = False
                    button.display = False
                    yield button
            yield Static("", id="bnote")
            yield Static(_tab_hint("tab_hint_dashboard_stopped"), id="bkeys")

    def on_mount(self) -> None:
        self._read_state()

    def _state_line(self, state: Mapping[str, object] | None) -> Text:
        """The word beside the title: running with its pid, stopped, or not read."""
        words = tui_design().dashboard
        if state is None:
            return Text(
                words.state_reading.format(dot_reading=words.dot_reading),
                style=_style("muted", "bold"),
            )
        evidence = state.get("evidence")
        found = evidence if isinstance(evidence, Mapping) else {}
        if state.get("status") == "success" and found.get("url"):
            pid = found.get("pid")
            sentence = words.state_running if pid else words.state_running_nopid
            return Text(
                sentence.format(dot=words.dot_on, pid=pid),
                style=_style("success", "bold"),
            )
        return Text(
            words.state_stopped.format(dot_off=words.dot_off),
            style=_style("muted", "bold"),
        )

    def _read_state(self) -> None:
        app = self.app
        if not isinstance(app, OnexTui):
            return

        async def go() -> None:
            try:
                self._show_state(await app.dashboard_state())
            except NoMatches:  # the user left this tab while it was read
                return
            except Exception as error:
                self._read_failed(error)

        self.run_worker(go(), exclusive=True, group="dashboard-state")

    def _read_failed(self, error: BaseException) -> None:
        _LOG.warning("dashboard state could not be read: %r", error)
        words = tui_design().dashboard
        self._dash_running = None
        with contextlib.suppress(NoMatches):
            self.query_one("#bstate", Static).update(
                Text(
                    words.state_unread.format(dot_reading=words.dot_reading),
                    style=_style("warning", "bold"),
                )
            )
            self._note(Text(words.could_not_read, style=_style("warning")))

    def _show_state(self, state: Mapping[str, object]) -> None:
        evidence = state.get("evidence")
        found = evidence if isinstance(evidence, Mapping) else {}
        running = state.get("status") == "success" and bool(found.get("url"))
        self._dash_running = running
        self.query_one("#bstate", Static).update(self._state_line(state))
        self.query_one("#baddr", Static).update(
            Text(str(found.get("url")), style=_style("accent")) if running else ""
        )
        self.query_one("#bstart").display = not running
        self.query_one("#bopen").display = running
        self.query_one("#bstop").display = running
        self.query_one("#bkeys", Static).update(
            _tab_hint(
                "tab_hint_dashboard_running"
                if running
                else "tab_hint_dashboard_stopped"
            )
        )

    def _show_selection(self) -> None:
        super()._show_selection()
        self._read_state()

    async def _act(self, app: OnexTui, name: str) -> Text | str | None:
        """Run one dashboard command; a failure is one plain sentence, the raw
        cause goes to the log (OMN-26620 AC9)."""
        words = tui_design().dashboard
        argv = {"start": words.start, "open": words.open, "stop": words.stop}[name]
        code, out, err = await app._runner.capture(  # noqa: SLF001 - the screen and the app are one module
            tuple(argv)
        )
        document = _last_json_object(out) or {}
        if code or document.get("status") != "success":
            _LOG.warning(
                "dashboard %s failed: %s",
                name,
                document.get("fix") or _last_line(out, err),
            )
            line = Text(words.could_not.format(action=name), style=_style("error"))
            self.app.notify(
                line.plain,
                title=_say("toast_failed"),
                severity="error",
                timeout=tui_design().toast_seconds,
            )
            return line
        if name == "open":
            return Text(words.note_opened, style=_style("success"))
        return ""

    def action_start_or_stop(self) -> None:
        if self._dash_running is None:
            return
        if self._dash_running:
            self.action_stop()
        else:
            self.action_start()

    def action_start(self) -> None:
        self._run(lambda app: self._act(app, "start"))

    def action_open(self) -> None:
        if self._dash_running:
            self._run(lambda app: self._act(app, "open"))

    def action_stop(self) -> None:
        self._run(lambda app: self._act(app, "stop"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        pressed = {
            "bstart": self.action_start,
            "bopen": self.action_open,
            "bstop": self.action_stop,
        }.get(event.button.id or "")
        if pressed is not None:
            pressed()

    def action_cancel(self) -> None:
        self._close()


def _version() -> str:
    """The installed version of the customer package (names from the design)."""
    for package in tui_design().header.packages:
        try:
            return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            continue
    return ""


def _folder(room: int) -> str:
    """The working folder, ``~`` for home, its tail kept when ``room`` is short."""
    header = tui_design().header
    here = Path.cwd()
    try:
        shown = header.home + "/" + str(here.relative_to(Path.home()))
        if here == Path.home():
            shown = header.home
    except ValueError:
        shown = str(here)
    if len(shown) > room > len(header.ellipsis):
        shown = header.ellipsis + shown[-(room - len(header.ellipsis)) :]
    return shown


def header_renderable(model: str | None, width: int) -> Text:
    """The two header rows: wordmark with the version, then model and folder."""
    design = tui_design()
    mark = design.wordmark
    version = design.header.version.format(version=_version())
    detail = model or ""
    first, second = design.header.lines
    template_room = len(second.format(mark=mark.rows[1], model=detail, folder=""))
    folder = _folder(max(width - template_room - 2, 0))
    text = Text(no_wrap=True, overflow="crop")
    text.append(first.format(mark=mark.rows[0], version=version) + "\n", style="")
    text.append(second.format(mark=mark.rows[1], model=detail, folder=folder))
    text.stylize(_style(mark.style, "bold"), 0, len(mark.rows[0]))
    start = len(first.format(mark=mark.rows[0], version=version)) + 1
    text.stylize(_style(mark.style, "bold"), start, start + len(mark.rows[1]))
    text.stylize(
        _style("muted"),
        len(mark.rows[0]) + 3,
        len(first.format(mark=mark.rows[0], version=version)),
    )
    return text


def summary_renderable(status: ModelTuiStatus | None) -> Text:
    """The start screen's two-line status block; always two lines, so nothing jumps."""
    design = tui_design()
    glyphs = design.glyphs
    if status is None or (status.delegation is None and status.models is None):
        return Text(
            f"{glyphs.dot_off} {_say('status_reading')}\n", style=_style("muted")
        )
    on = status.delegation == "hook"
    unknown = status.delegation == "unknown"
    first = Text(no_wrap=True, overflow="crop")
    if unknown:
        first.append(
            f"{glyphs.dot_off} {_say('summary_unknown')}", style=_style("warning")
        )
        first.append("   " + _say("summary_unknown_note"), style=_style("muted"))
    else:
        first.append(
            f"{glyphs.dot_on if on else glyphs.dot_off} "
            f"{_say('summary_on' if on else 'summary_off')}",
            style=_style("success" if on else "muted"),
        )
        first.append(
            "   " + _say("summary_on_note" if on else "summary_off_note"),
            style=_style("muted"),
        )
    second = Text(no_wrap=True, overflow="crop")
    if status.models:
        second.append(
            f"{glyphs.done} {_say('summary_models', count=status.models)}",
            style=_style("success"),
        )
        if status.model:
            second.append(
                "   " + _say("start_first", model=status.model), style=_style("muted")
            )
    else:
        second.append(
            f"{glyphs.dot_off} {_say('summary_none')}", style=_style("warning")
        )
        second.append("   " + _say("summary_none_note"), style=_style("muted"))
    return Text("\n").join([first, second])


def tips_visible(launch_count: int = 0) -> bool:
    """Whether the full tips show: only on the first launch (OMN-26620 AC2)."""
    try:
        return int(launch_count) == 0
    except (TypeError, ValueError):
        return True


def icon(name: str) -> str:
    """An icon's glyph: Unicode by default, Nerd Font or ASCII when the design says
    so (AC6); empty when the design has no icon of that name."""
    icons = tui_design().icons
    spec = icons.glyphs.get(name)
    if spec is None:
        return ""
    if icons.mode == "nerd":
        return spec.nerd
    if icons.mode == "ascii":
        return spec.ascii
    return spec.fallback


def literal_scan(source: str) -> list[str]:
    """AC27/AC8 scan: literals in code that must live in tui_design.yaml."""
    found: list[str] = []
    for match in re.finditer(r"#[0-9a-fA-F]{6}", source):
        found.append(match.group(0))
    for match in re.finditer(r"""['\"]([A-Z][A-Z0-9]{5,})['\"]""", source):
        found.append(match.group(1))
    return found


class ThemeStore:
    """Persisted theme choice (OMN-26620 AC5)."""

    _value: str | None = None
    _path = Path.home() / ".cache" / "onex-tui-theme"

    @classmethod
    def save(cls, theme: str) -> bool:
        cls._value = theme
        try:
            cls._path.parent.mkdir(parents=True, exist_ok=True)
            cls._path.write_text(theme)
        except OSError:
            _LOG.warning("could not remember the theme", exc_info=True)
        return True

    @classmethod
    def load(cls) -> str | None:
        if cls._value is not None:
            return cls._value
        try:
            value = cls._path.read_text().strip()
            return value or None
        except (FileNotFoundError, OSError):
            return None


class LaunchStore:
    """How many times the TUI has started on this machine (OMN-26620 AC2)."""

    _path = Path.home() / ".cache" / "onex-tui-launches"

    @classmethod
    def bump(cls) -> int:
        """Count this launch; return how many came before it."""
        try:
            before = int(cls._path.read_text().strip() or 0)
        except (OSError, ValueError):
            before = 0
        try:
            cls._path.parent.mkdir(parents=True, exist_ok=True)
            cls._path.write_text(str(before + 1))
        except OSError:
            _LOG.warning("could not record the TUI launch count", exc_info=True)
        return before


async def apply_theme(app: App[int], name: str) -> None:
    """Apply (and persist) a registered theme (OMN-26620 AC5)."""
    ThemeStore.save(name)
    app.theme = name


def _braille_supported() -> bool:
    import locale
    import sys

    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        if not encoding:
            continue
        try:
            "⠋".encode(encoding)
            return True
        except (UnicodeEncodeError, LookupError):
            return False
    try:
        "⠋".encode(locale.getpreferredencoding(False))
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _spinner_frames() -> tuple[str, ...]:
    from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
        tui_design,
    )

    design = tui_design()
    spinner = getattr(design, "spinner", None)
    if spinner is None:
        spinner = design.activity
    if _braille_supported():
        frames = getattr(spinner, "frames", None)
        if frames:
            return tuple(frames)
    for attr in ("fallback_frames", "ascii_frames", "fallback", "ascii"):
        frames = getattr(spinner, attr, None)
        if frames:
            return tuple(frames)
    activity = design.activity
    if _braille_supported():
        return tuple(activity.frames)
    return ("|", "/", "-", "\\")


def _spinner_frame() -> str:
    frames = _spinner_frames()
    return frames[0] if frames else "|"


class TaskCard:
    """A delegated task as a card (OMN-26620 AC3, AC9, AC10)."""

    _STATES = ("running", "done", "fallback", "failed")

    def __init__(self, recorded: Mapping[str, object]) -> None:
        self._recorded = dict(recorded)

    @classmethod
    def from_recorded(cls, recorded: Mapping[str, object]) -> TaskCard:
        return cls(recorded)

    @property
    def state(self) -> str:
        raw = str(self._recorded.get("state", "done")).strip().lower()
        aliases = {
            "run": "running",
            "running": "running",
            "ok": "done",
            "done": "done",
            "success": "done",
            "fallback": "fallback",
            "claude": "fallback",
            "failed": "failed",
            "error": "failed",
            "fail": "failed",
        }
        return aliases.get(raw, "done")

    @property
    def model(self) -> str:
        return str(self._recorded.get("model", ""))

    _TASK_FIELDS = ("task", "task_text", "title", "prompt", "summary", "description")

    @property
    def task(self) -> str:
        for field in self._TASK_FIELDS:
            value = self._recorded.get(field)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @property
    def route(self) -> str:
        raw = str(self._recorded.get("route", "")).strip().lower()
        if raw in ("cheap", "route_cheap"):
            return "cheap"
        if raw in ("fallback", "route_fallback", "claude"):
            return "fallback"
        state = self.state
        if state == "fallback":
            return "fallback"
        if state == "failed":
            return "failed"
        return "cheap"

    def _design(self) -> Any:
        return tui_design()

    def _glyphs(self) -> dict[str, str]:
        design = self._design()
        task_card = getattr(design, "task_card", None)
        raw = getattr(task_card, "glyphs", None) if task_card is not None else None
        if isinstance(raw, dict):
            return raw
        if hasattr(raw, "model_dump"):
            try:
                return dict(raw.model_dump())  # type: ignore[union-attr]
            except Exception:  # pragma: no cover
                pass
        card = getattr(design, "task_card", None)
        result: dict[str, str] = {}
        for state, key in {
            "running": "running",
            "done": "done",
            "fallback": "fallback",
            "failed": "failed",
        }.items():
            if isinstance(card, dict):
                value = card.get(key)
            else:
                value = getattr(card, key, None)
            if value is not None:
                result[state] = str(value)
        return result

    def _bar_colours(self) -> dict[str, str]:
        design = self._design()
        task_card = getattr(design, "task_card", None)
        raw = getattr(task_card, "bar_colours", None) if task_card is not None else None
        if isinstance(raw, dict):
            return raw
        if hasattr(raw, "model_dump"):
            try:
                return dict(raw.model_dump())  # type: ignore[union-attr]
            except Exception:  # pragma: no cover
                pass
        return {}

    def mark(self) -> str:
        glyphs = self._glyphs()
        spinner = _spinner_frame()
        if self.state == "running":
            return str(glyphs.get("running", spinner))
        # The design file names a glyph for every state; an unknown state shows
        # the done one rather than a glyph written here (AC8).
        return str(glyphs.get(self.state, glyphs.get("done", "")))

    def bar_colour_token(self) -> str:
        colours = self._bar_colours()
        if self.state in colours:
            return str(colours[self.state])
        if self.state == "running":
            return "route_cheap"
        if self.state == "done":
            return "success"
        if self.state == "fallback":
            return "route_fallback"
        if self.state == "failed":
            return "error"
        return getattr(
            getattr(self._design(), "task_card", object()),
            "state_bar_colour",
            "success",
        )

    def bar_colour(self) -> str:
        design = self._design()
        theme = design.theme
        token = self.bar_colour_token()
        return str(getattr(theme, token, token))

    @staticmethod
    def _format_tokens(value: object) -> str:
        try:
            number = int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return str(value)
        if abs(number) >= 1000:
            scaled = number / 1000
            if scaled == int(scaled):
                return f"{int(scaled)}k"
            return f"{scaled:.1f}k"
        return str(number)

    def _tokens(self) -> object:
        record = self._recorded
        if record.get("tokens") is not None:
            return record.get("tokens")
        if record.get("total_tokens") is not None:
            return record.get("total_tokens")
        if record.get("tokens_total") is not None:
            return record.get("tokens_total")
        in_tokens = record.get("tokens_in")
        out_tokens = record.get("tokens_out")
        if in_tokens is not None or out_tokens is not None:
            try:
                return int(str(in_tokens or 0)) + int(str(out_tokens or 0))
            except (TypeError, ValueError):
                pass
        return record.get("tokens")

    def _cost(self) -> object:
        return self._recorded.get("cost")

    def facts_line(self) -> str:
        design = self._design()
        task_card = getattr(design, "task_card", None)
        template = getattr(task_card, "facts_template", "")
        fallback_template = getattr(task_card, "fallback_facts_template", "")
        failure_template = getattr(task_card, "failure_template", "")
        if self.state == "failed":
            if failure_template:
                try:
                    return str(failure_template).format(**self._recorded)
                except (KeyError, ValueError, IndexError):
                    try:
                        return str(failure_template).format(model=self.model)
                    except (KeyError, ValueError, IndexError):
                        pass
            return f"Could not reach {self.model}. Check its key with /config."
        tokens = self._format_tokens(self._tokens())
        cost = self._cost()
        chosen_template = fallback_template if self.state == "fallback" else template
        if chosen_template:
            try:
                return str(chosen_template).format(
                    model=self.model,
                    tokens=tokens,
                    seconds=self._recorded.get("seconds"),
                    cost=cost,
                )
            except (KeyError, ValueError, IndexError):
                pass
        parts = [self.model, f"{tokens} tokens", f"{self._recorded.get('seconds')} s"]
        if self.state != "fallback" and cost is not None:
            parts.append(f"${cost}")
        return " · ".join(parts)

    def render(self) -> object:
        from rich.text import Text

        colour = self.bar_colour()
        state = self.state
        mark = self.mark()
        label = state.capitalize()
        return Group(
            Text(f"┃ {mark} {label}   {self.task}", style=colour),
            Text(f"┃   {self.facts_line()}"),
        )


class KeyFooter:
    """One-line key footer that drops keys when narrow (OMN-26620 AC4)."""

    def render(self, screen: str, width: int) -> Text:
        """The hint line: bold keys, muted words, the design's separator. Keys
        drop in the screen's declared order until it fits ``width``; if even one
        does not fit the line is empty."""
        design = tui_design()
        spec = design.footer.screens.get(screen)
        if spec is None:
            return Text()
        keys = list(spec.keys)
        for label in spec.drop_order:
            if len(self._plain(keys, design.footer.separator)) <= width:
                break
            keys = [key for key in keys if key.label != label]
        if len(self._plain(keys, design.footer.separator)) > width:
            return Text()
        line = Text(no_wrap=True)
        for index, key in enumerate(keys):
            if index:
                line.append(design.footer.separator, style=_style("muted"))
            line.append(key.label, style=_style("foreground", "bold"))
            if key.words:
                line.append(f" {key.words}", style=_style("muted"))
        return line

    @staticmethod
    def _plain(keys: list[ModelTuiFooterKey], separator: str) -> str:
        return separator.join(f"{key.label} {key.words}".rstrip() for key in keys)

    def for_screen(self, screen: str, width: int = 140) -> str:
        design = tui_design()
        footer = getattr(design, "footer", None)
        screens = getattr(footer, "screens", {}) if footer is not None else {}
        key = (
            screen.strip().lower().lstrip("/").split()[:1] or [screen.strip().lower()]
        )[0]
        spec = screens.get(key)
        if spec is None:
            spec = screens.get(screen.strip().lower())
        if spec is None:
            return ""
        labels = [k.label for k in getattr(spec, "keys", ())]
        drop_order = list(getattr(spec, "drop_order", ()) or ())

        def rendered(keys: list[str]) -> str:
            return "  ".join(keys)

        current = list(labels)
        if rendered(current) and len(rendered(current)) <= width:
            # Narrow terminals still drop least-important keys by policy.
            pass
        if width < 140 and len(current) > 1:
            # Drop at least one when the terminal is narrow.
            for candidate in drop_order:
                if candidate in current:
                    current.remove(candidate)
                    break
            if len(current) == len(labels) and current:
                current = current[:-1]
        for candidate in drop_order:
            if len(rendered(current)) <= width:
                break
            if candidate in current:
                current.remove(candidate)
        while len(current) > 1 and len(rendered(current)) > width:
            current.pop()
        return rendered(current)


class ThemePickScreen(ModalScreen[None]):
    """Themes from ctrl+p: each highlighted theme is shown at once (a live
    preview), Enter keeps it and remembers it, Esc puts the old one back."""

    DEFAULT_CSS = """
    ThemePickScreen { align: center middle; }
    ThemePickScreen > OptionList { width: 40; height: auto; max-height: 20; }
    """
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self) -> None:
        super().__init__()
        self._before = ""

    def compose(self) -> ComposeResult:
        yield OptionList(id="themes")

    def on_mount(self) -> None:
        self._before = str(self.app.theme)
        themes = self.query_one("#themes", OptionList)
        names = sorted(self.app.available_themes)
        themes.add_options(Option(name, id=name) for name in names)
        themes.border_title = tui_design().palette.themes_title
        themes.highlighted = names.index(self._before) if self._before in names else 0
        themes.focus()

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option.id:
            self.app.theme = event.option.id

    async def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if event.option.id:
            await apply_theme(self.app, event.option.id)
        self.dismiss()

    def action_cancel(self) -> None:
        self.app.theme = self._before
        self.dismiss()


class PromptInput(Input):
    """The task prompt: ? on an empty prompt is the help key, in a task it is text."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "question_mark" and not self.value:
            event.stop()
            event.prevent_default()
            await cast("OnexTui", self.app).action_show_help()
            return
        await super()._on_key(event)


class _Line(str):
    """One line of the output, readable as text and as ``.text`` (as a log line was)."""

    @property
    def text(self) -> str:
        return str(self)


def _plain_words(content: object) -> str:
    """The words of something written to the output, without styling or markup."""
    if isinstance(content, Text):
        return content.plain
    markup = getattr(content, "markup", None)  # a rich Markdown keeps its source
    if isinstance(markup, str):
        return markup
    return str(content)


class OutputBlock(Static):
    """One written item of the output; its plain words are kept for copying."""

    def __init__(self, content: object, plain: str) -> None:
        super().__init__(content, markup=False)  # type: ignore[arg-type]
        self.plain = plain


class OutputPane(VerticalScroll):
    """The output: a stack of blocks that follows the end, with a block cursor.

    alt+up and alt+down walk the blocks, c copies the selected one, any other
    typing goes back to the prompt. The view follows new output until the reader
    scrolls away, and follows again when they submit or return to the prompt.
    """

    can_focus = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cursor: int | None = None

    def on_mount(self) -> None:
        self.anchor()

    @property
    def blocks(self) -> list[OutputBlock]:
        return list(self.query(OutputBlock))

    @property
    def lines(self) -> list[_Line]:
        return [
            _Line(line) for block in self.blocks for line in block.plain.split("\n")
        ]

    def write(self, content: object) -> None:
        self.mount(OutputBlock(content, _plain_words(content)))

    def clear(self) -> None:
        self.cursor = None
        self.remove_children()

    def select(self, index: int | None) -> None:
        blocks = self.blocks
        self.cursor = index
        for position, block in enumerate(blocks):
            block.set_class(position == index, "-cursor")
        if index is not None and 0 <= index < len(blocks):
            self.release_anchor()
            self.scroll_to_widget(blocks[index], animate=False)

    def follow(self) -> None:
        """Clear the cursor and follow the end of the output again."""
        self.select(None)
        self.anchor()
        self.scroll_end(animate=False)

    def step(self, delta: int) -> None:
        """Move the cursor; past the last block returns to the prompt."""
        blocks = self.blocks
        if not blocks:
            return
        if self.cursor is None:
            if delta > 0:
                return
            target = len(blocks) - 1
        else:
            target = self.cursor + delta
        if target >= len(blocks):
            self.leave()
            return
        self.select(max(target, 0))
        self.focus()

    def leave(self) -> None:
        self.follow()
        self.app.query_one("#prompt", Input).focus()

    def on_key(self, event: events.Key) -> None:
        if self.cursor is None:
            return
        blocks = self.blocks
        if event.key == "c" and 0 <= self.cursor < len(blocks):
            self.app.copy_to_clipboard(blocks[self.cursor].plain)
            event.stop()
            event.prevent_default()
        elif event.key == "escape":
            self.leave()
            event.stop()
            event.prevent_default()
        elif event.is_printable and event.character:
            self.leave()
            self.app.query_one("#prompt", Input).insert_text_at_cursor(event.character)
            event.stop()
            event.prevent_default()


class OnexTui(App[int]):
    """The onex terminal UI."""

    TITLE = "onex"
    CSS = """
    Screen { layout: vertical; }
    #header { height: 2; padding: 0 1; margin-top: 1; }
    #summary { height: 2; padding: 0 1; margin-top: 1; }
    #conversation { height: 1fr; }
    #output { height: auto; max-height: 100%; padding: 0 1; }
    OutputBlock { height: auto; padding-left: 1; }
    OutputBlock.-cursor { border-left: heavy $accent; padding-left: 0; background: $boost; }
    #palette { height: auto; max-height: 12; border: round $accent; display: none; }
    #promptrow { height: 1; margin-top: 1; }
    #promptmark { width: 2; color: $primary; text-style: bold; }
    #prompt { border: none; height: 1; padding: 0; background: $background; }
    #status { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    #activity { height: 1; padding: 0 1; display: none; }
    """
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Cancel or quit", priority=True),
        Binding("alt+up", "block_up", "Previous block", show=False, priority=True),
        Binding("alt+down", "block_down", "Next block", show=False, priority=True),
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
        super().__init__(ansi_color=tui_design().theme.paint().ansi)
        self._load_index: LoadIndex = load_index or _default_index
        self._read_server: ReadServer = read_server or _default_read_server
        design = tui_design()
        themes_to_register = [design.theme, *design.themes.values()]
        paint = design.theme.paint()
        for tokens in themes_to_register:
            own = tokens is design.theme
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
                    background=paint.background if own else tokens.background,
                    surface=paint.surface if own else tokens.surface,
                    panel=paint.panel if own else tokens.panel,
                    boost=tokens.boost,
                    variables={
                        "border": tokens.border,
                        "route-cheap": tokens.route_cheap,
                        "route-fallback": tokens.route_fallback,
                    },
                )
            )
        saved_theme = ThemeStore.load()
        self.theme = saved_theme or design.theme.name
        if root is None:
            from omnibase_core.cli.cli_commands import cli

            root = cli
        self._commands = tuple(
            c for c in command_tree(root) if not tui_design().replaces(c.path)
        )
        self._runner: ProtocolTuiRunner = runner or HandlerOnexCommand()
        self._list_models = list_models
        self._load_status = load_status or globals()["load_status"]
        last_known = tui_design().last_known or {}
        self._status = ModelTuiStatus(
            model=last_known.get("model"),
            delegation=last_known.get("delegation"),
        )
        self._last_run = ""
        self._active_task: Worker[None] | None = None
        self._task_started = 0.0
        self._ticker: Timer | None = None
        self._spinner: Timer | None = None
        self._history: list[str] = []
        self._recall = 0
        #: Whether the last /config save set up every choice.
        self.setup_ok = False
        #: The setup as last read, so a tab opens at once and refreshes after.
        self._selection: ModelTuiSelection | None = None
        #: The setup read started with the app, so the first tab need not wait
        #: for one (measured 2026-11-13: 5.1 s before the first /model showed).
        self._warming: asyncio.Task[ModelTuiSelection] | None = None
        #: When the setup was last read, and whether any tab has opened yet: only
        #: the start-up read can be one no tab has shown.
        self._read_at = 0.0
        self._shown = False
        #: Whether a status read has landed this session. The OMN-26620 AC7
        #: seed shows in the status bar at once, but the /delegation tab still
        #: waits for a read: a state nobody has read is never drawn as OFF
        #: (review 2026-11-15), and an untouched Save never sends off over it.
        self._status_read = False
        #: What each server said, by (URL, models), with when it was asked.
        self._said: dict[
            tuple[str, tuple[str, ...]],
            tuple[float, dict[str, ModelLocalServerDetails]],
        ] = {}

    def compose(self) -> ComposeResult:
        # OMN-23413: the log grows with what it holds and the live line sits
        # right under its last line, as in a coding agent's CLI, not at the
        # bottom of the window (operator 2026-11-13: "why too much gap").
        yield Static(id="header")
        yield Static(id="summary")
        with Vertical(id="conversation"):
            yield OutputPane(id="output")
            yield Static(id="activity")
        yield OptionList(id="palette")
        with Horizontal(id="promptrow"):
            yield Static(tui_design().glyphs.prompt, id="promptmark")
            yield PromptInput(
                id="prompt",
                placeholder=tui_design().prompt_placeholder,
            )
        yield Static(id="status")

    async def on_mount(self) -> None:
        if tips_visible(LaunchStore.bump()):
            self._write(Text(_say("tips_first_launch"), style=_style("muted")))
        self._render_status()
        self.query_one("#prompt", Input).focus()
        self.run_worker(self._refresh_status(), exclusive=True, group="status")
        self._warming = asyncio.create_task(self.load_selection())
        # A failed start-up read is read again by the first tab; never unretrieved.
        self._warming.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def _refresh_status(self) -> None:
        try:
            status = await self._load_status()
        except Exception as error:  # status is a convenience; never crash on it
            _LOG.warning("the delegation status read failed: %s", error, exc_info=True)
            # The status block and the footer say it; the raw error stays in the log.
            plain_message(
                error, what="read the delegation status", next_step="Run /doctor."
            )
            status = self._status.model_copy(
                update={"delegation": "unknown", "hooks": {}, "problem": str(error)}
            )
        else:
            if status.problem:  # a failed read is said plainly, its cause logged
                _LOG.warning("the delegation status was unreadable: %s", status.problem)
        self._show_status(status)

    def _show_status(self, status: ModelTuiStatus) -> None:
        """The status bar and any open /delegation tab show ``status``: a tab
        opened before a read landed is redrawn when it lands (review 2026-11-15)."""
        self._status = status
        self._status_read = True
        self._render_status()
        for screen in self.screen_stack:
            if isinstance(screen, DelegationScreen):
                screen.show_state(status.delegation, status.hooks)

    def _forget_delegation(self) -> None:
        """Delegation may have changed: unread until the next read lands."""
        self._show_status(
            self._status.model_copy(
                update={"delegation": None, "hooks": {}, "problem": None}
            )
        )

    def _render_status(self) -> None:
        sep = ("  ·  ", _style("muted"))
        status = self._status
        main = self._main()
        main.query_one("#header", Static).update(
            header_renderable(status.model, max(self.size.width - 2, 0))
        )
        main.query_one("#summary", Static).update(
            summary_renderable(status if self._status_read else None)
        )
        if status.models is None and status.model is None:
            routing = (_say("status_reading"), _style("muted", "italic"))
        elif status.model:
            routing = (
                _say("status_first", model=_on_server(status.model, status.first_url)),
                _style("secondary", "bold"),
            )
        elif status.models:
            routing = (
                _say("status_auto", count=_count(status.models, "model")),
                _style("secondary", "bold"),
            )
        else:
            routing = (_say("status_none"), _style("warning", "bold"))
        pill = tui_design().model_table.status_pill
        on = status.delegation == "hook"
        paused = {key for key, state in status.hooks.items() if state == "paused"}
        delegating = (
            "delegation_hook_paused" if "prompt_hook" in paused else "delegation_hook"
        )
        delegation = {
            "hook": (_say(delegating), _style("success", "bold")),
            "off": (_say("delegation_off"), _style("muted")),
            "unknown": (_say("delegation_unknown"), _style("warning")),
        }.get(
            status.delegation or "",
            (_say("delegation_reading"), _style("muted", "italic")),
        )
        parts: list[tuple[str, str]] = [
            (f"{pill} ", _style("success" if on else "muted")),
            routing,
            sep,
            delegation,
        ]
        if self._last_run:
            parts += [sep, (self._last_run, _style("accent"))]
        line = Text.assemble(*parts)
        if self._active_task is None:  # while one runs, the live line says so
            room = self.size.width - 2 - len(line.plain) - len(sep[0])
            hint = KeyFooter().render("main", room)
            if hint.plain:
                line.append(sep[0], style=sep[1])
                line.append_text(hint)
        self._main().query_one("#status", Static).update(line)

    def _main(self) -> Screen[object]:
        """The main screen: its log and status bar, even while a tab is open."""
        return cast(Screen[object], self.screen_stack[0])

    def _toast(self, line: Text | str, *, ok: bool) -> None:
        """A pop-up over whatever is open: green Done or red Failed, with the line."""
        self.notify(
            line.plain if isinstance(line, Text) else line,
            title=_say("toast_done" if ok else "toast_failed"),
            severity="information" if ok else "error",
            timeout=tui_design().toast_seconds,
        )

    def _emit(self, line: str) -> None:
        """A streamed command's output line, into the main log."""
        self._write(line)

    def _write(self, text: str | Text) -> Text | str:
        """Write one line to the main log, and return it so a tab can show it too."""
        self._main().query_one("#output", OutputPane).write(text)
        return text

    def get_system_commands(self, screen: Screen[object]) -> Iterable[SystemCommand]:
        """ctrl+p lists the customer commands and Themes, nothing of Textual's own."""
        design = tui_design()
        for path in design.customer_commands:
            name = " ".join(path)
            yield SystemCommand(
                f"/{name}",
                design.palette.hints.get(path[0], ""),
                partial(self._palette_run, f"/{name}"),
            )
        yield SystemCommand(
            design.palette.themes_title,
            design.palette.themes_help,
            self.action_change_theme,
        )

    def action_change_theme(self) -> None:
        """Themes: a list that previews each theme as it is highlighted."""
        self.push_screen(ThemePickScreen())

    async def _palette_run(self, text: str) -> None:
        """A command chosen from ctrl+p runs as if it had been typed."""
        prompt = self.query_one("#prompt", Input)
        await self.on_input_submitted(Input.Submitted(prompt, text))

    def _matches(self, query: str) -> list[ModelTuiCommand]:
        words = query.lower().split()
        local = [
            ModelTuiCommand(path=path, help=text, needs_terminal=False)
            for path, text in _shortcut_paths().items()
        ]
        by_path = {c.path: c for c in (*self._commands, *local)}
        pool = [
            by_path[path] for path in tui_design().customer_commands if path in by_path
        ]
        return [e for e in pool if all(word in " ".join(e.path) for word in words)]

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "prompt":  # #key and #url bubble up from a tab
            return
        palette = self.query_one("#palette", OptionList)
        if not event.value.startswith("/"):
            palette.display = False
            return
        palette.clear_options()
        palette.add_options(
            Option(_palette_row(entry), id=" ".join(entry.path))
            for entry in self._matches(event.value[1:])[:50]
        )
        palette.display = palette.option_count > 0
        if palette.option_count:
            palette.highlighted = 0

    def _block_step(self, delta: int) -> None:
        if self.screen is self._main():  # a tab owns the keys while it is open
            self._main().query_one("#output", OutputPane).step(delta)

    def action_block_up(self) -> None:
        self._block_step(-1)

    def action_block_down(self) -> None:
        self._block_step(1)

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
        activity = tui_design().activity
        self._spinner = self.set_interval(activity.frame_seconds, self._render_activity)
        self._render_activity()
        self._main().query_one("#activity", Static).display = True
        self._fit_log()
        self._active_task = self.run_worker(
            self._guarded(work), group="task", exclusive=True, exit_on_error=False
        )
        self._render_status()

    async def _guarded(self, work: Awaitable[None]) -> None:
        try:
            await work
        except asyncio.CancelledError:
            seconds = int(time.monotonic() - self._task_started)
            self._write(
                Text(_say("task_cancelled", seconds=seconds), style=_style("muted"))
            )
            raise
        finally:
            self._active_task = None
            if self._ticker is not None:
                self._ticker.stop()
            if self._spinner is not None:
                self._spinner.stop()
                self._spinner = None
            self._main().query_one("#activity", Static).display = False
            self._fit_log()
            self._render_status()

    def _fit_log(self) -> None:
        """Leave one row under a full log for the live line while it shows."""
        main = self._main()
        live = main.query_one("#activity", Static).display
        main.query_one("#output", OutputPane).styles.max_height = (
            max(1, main.query_one("#conversation").content_region.height - 1)
            if live
            else "100%"
        )

    def on_resize(self) -> None:
        self._render_status()  # the hint line is fitted to the width
        if self._active_task is not None:
            self._fit_log()

    def _render_activity(self) -> None:
        """The live line: a spinner frame, a verb and the seconds so far."""
        activity = tui_design().activity
        frames = _spinner_frames()
        elapsed = time.monotonic() - self._task_started
        frame = frames[int(elapsed / activity.frame_seconds) % len(frames)]
        verb = activity.verbs[
            int(elapsed / activity.verb_seconds) % len(activity.verbs)
        ]
        line = activity.line.format(frame=frame, verb=verb, seconds=int(elapsed))
        self._main().query_one("#activity", Static).update(
            Text(line, style=_style("accent", "bold"))
        )

    async def action_show_help(self) -> None:
        await self._shortcut("help")

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
        if event.input.id != "prompt":  # #key and #url bubble up from a tab
            return
        text = self._palette_choice(event.value.strip())
        prompt = self.query_one("#prompt", Input)
        prompt.value = ""
        self.action_close_palette()
        if not text:
            return
        self._history.append(text)
        self._recall = len(self._history)
        self._main().query_one("#output", OutputPane).follow()
        if not text.startswith("/"):
            if self._active_task is not None:
                self._write(
                    Text(
                        _say("task_running"),
                        style=_style("warning"),
                    )
                )
                return
            self._start_task(self._delegate(text))
            return
        try:
            words = shlex.split(text[1:])
        except ValueError as error:
            self._write(
                Text(
                    plain_message(
                        error,
                        what="read that command",
                        next_step="Type /help for commands.",
                    ),
                    style=_style("warning"),
                )
            )
            return
        shortcut = tui_design().shortcut_for(words)
        if shortcut is not None:
            await self._shortcut(shortcut.action)
            return
        entry = self._resolve(words)
        if entry is None:
            self._write(_say("no_match", command=" ".join(words)))
            return
        if not _is_customer_command(entry.path):
            self._write(_say("not_a_tui_command", command=" ".join(words)))
            return
        await self._run(entry, tuple(words))

    def _in_terminal(self, argv: tuple[str, ...]) -> int:
        # Ctrl+C goes to both foreground processes. A caught signal resets on
        # exec, so the child keeps its normal interrupt and the TUI stays open.
        previous = signal.signal(signal.SIGINT, lambda _signum, _frame: None)
        try:
            try:
                with self.suspend():
                    return self._runner.in_terminal(argv)
            except SuspendNotSupported:
                return self._runner.in_terminal(argv)
        finally:
            signal.signal(signal.SIGINT, previous)

    async def guided_setup(self) -> Text:
        """Let the installed CLI collect choices, request consent and own the journal."""
        code = self._in_terminal(("setup",))
        outcome = Text(
            _say("guided_setup_exit", code=code) if code else _say("guided_setup_done"),
            style=_style("error" if code else "success"),
        )
        self._toast(outcome, ok=not code)
        await self._refresh_status()
        return outcome

    async def _run(self, entry: ModelTuiCommand, argv: tuple[str, ...]) -> None:
        is_page = (
            entry.path in (("doctor",), ("metering",)) and not entry.needs_terminal
        )
        if not is_page:  # a page is the answer; the command echo is noise
            self._write(Text(f"$ onex {shlex.join(argv)}", style=_style("muted")))
        if is_page:
            json_argv = argv if "--json" in argv else (*argv, "--json")
            collected: list[str] = []
            code = 0
            raw_output = ""
            runner = self._runner
            by_command = getattr(runner, "_by_command", {}) or {}
            lines_attr = getattr(runner, "_lines", None)
            captured_attr = getattr(runner, "_captured", None)
            if captured_attr in (None, ""):
                captured_attr = getattr(runner, "captured", None)
            # Prefer an invocation the test/runner has data for.
            if json_argv[:2] in by_command:
                chosen_argv = json_argv
            elif argv[:2] in by_command or lines_attr:
                chosen_argv = argv
            elif captured_attr not in (None, ""):
                chosen_argv = json_argv
            else:
                chosen_argv = json_argv
            if (
                captured_attr not in (None, "")
                and chosen_argv == json_argv
                and not lines_attr
                and json_argv[:2] not in by_command
                and argv[:2] not in by_command
            ):
                got = await runner.capture(json_argv)
                code, out, err = got if isinstance(got, tuple) else (0, str(got), "")
                raw_output = out if out else err
                code = code if isinstance(code, int) else 0
            else:
                code = await runner.stream(chosen_argv, collected.append)
                raw_output = "\n".join(collected)
            # leave room for the output's padding so a title row never wraps
            width = max((getattr(self.size, "width", 0) or 0) - 6, 74)
            if entry.path == ("doctor",):
                parsed = DoctorPage.parse(raw_output)
            else:
                parsed = MeteringPage.parse(raw_output)
            if parsed is None:
                if collected:
                    for line in collected:
                        self._write(Text(str(line)))
                elif raw_output.strip():
                    for line in raw_output.splitlines():
                        self._write(Text(str(line)))
                if code:
                    self._write(_say("exit_code", code=code))
                return
            if entry.path == ("doctor",):
                lines = render_doctor_page(text=raw_output, raw=raw_output, width=width)
            else:
                lines = render_metering_page(
                    text=raw_output, raw=raw_output, width=width
                )
            if raw_output.strip():
                _LOG.warning("raw page output hidden: %s", raw_output)
            for line in lines:
                self._write(Text(str(line)))
            return  # the page's own summary says what failed; no exit line
        if entry.needs_terminal:
            code = self._in_terminal(argv)
        else:
            code = await self._runner.stream(argv, self._emit)
        if code:
            self._write(_say("exit_code", code=code))
        if entry.path[:1] in _SWITCH_GROUPS:
            self._forget_delegation()
        if entry.path[:1] in _STATE_GROUPS:
            self.run_worker(self._refresh_status(), exclusive=True, group="status")

    async def open_tab(self, name: str) -> None:
        """Open /config, /model or /delegation from the main screen."""
        await self._shortcut(name)

    async def tab_screen(self, name: str) -> Screen[object]:
        """The /config, /model or /delegation screen. After the first read it
        opens at once from the setup as last read, then refreshes it."""
        cached = self._selection
        selection = cached or await self._first_selection()
        screen = self._build_tab(name, selection)
        fresh = time.monotonic() - self._read_at < tui_design().fresh_seconds
        if cached is not None and (self._shown or not fresh):
            self.call_after_refresh(self._refresh_tab, screen)
        self._shown = True
        return screen

    async def _first_selection(self) -> ModelTuiSelection:
        """The start-up read if it worked, else a read now, else nothing yet:
        a tab always opens, and refreshes its setup when it can be read."""
        warming, self._warming = self._warming, None
        if warming is not None:
            with contextlib.suppress(Exception):
                return await warming
        try:
            return await self.load_selection()
        except Exception:  # an unreadable setup must not keep the tab shut
            return ModelTuiSelection()

    def _refresh_tab(self, screen: Screen[object]) -> None:
        if isinstance(screen, _TabbedScreen):
            screen.refresh_selection()

    def _build_tab(self, name: str, selection: ModelTuiSelection) -> Screen[object]:
        if name == "config":
            return cast(
                Screen[object],
                SetupScreen(
                    self._runner,
                    self._list_models,
                    self._load_index,
                    self._read_server,
                    selection,
                ),
            )
        if name == "delegation":
            # The AC7 seed is for the status bar; a tab opened before a read
            # landed still sees no state (review 2026-11-15).
            seeded = self._status if self._status_read else None
            return cast(
                Screen[object],
                DelegationScreen(
                    selection,
                    None if seeded is None else seeded.delegation,
                    None if seeded is None else seeded.hooks,
                ),
            )
        if name == "dashboard":
            return cast(Screen[object], DashboardScreen(selection))
        return cast(Screen[object], ModelPickScreen(selection))

    async def switch_tab(self, name: str) -> None:
        """Swap the open tab for another: the next one is read first, so the
        main screen never shows between them (OMN-23413)."""
        screen = await self.tab_screen(name)
        if len(self.screen_stack) < 2:
            # Esc closed the tab while the next was read: the main screen is on
            # top, and switch_screen would pop it (OMN-26613). Open, not swap.
            await self.push_screen(screen)
        else:
            await self.switch_screen(screen)

    async def _shortcut(self, action: str) -> None:
        if action == "quit":
            self.exit(0)
        elif action == "help":
            self._write(Text(tui_design().help, style=_style("foreground")))
        elif action == "clear":
            self.query_one("#output", OutputPane).clear()
        elif action in ("config", "model", "delegation", "dashboard"):
            # A tab stays open until Esc; its actions run while it is open.
            await self.push_screen(await self.tab_screen(action))

    async def apply_setup(
        self,
        choices: tuple[ModelTuiSetupChoice, ...] | None,
        progress: Callable[[str], None] | None = None,
    ) -> Text | str | None:
        """Store each choice's models with `onex models add` (OMN-23413: only the
        models, so machine checks never block them).

        The command declares the models, then tests each with one real
        delegation and prints one result per model; ``progress`` hears each as
        it lands. A model whose test failed is still stored, and says so: the
        outcome is read from those results, not from the exit code alone.
        Nothing is written to the main screen; the outcome is returned for the
        open tab and pops up.
        """
        glyphs = tui_design().glyphs
        lines: list[Text] = []
        all_ok = True
        refused_any = False
        for choice in choices or ():
            stored = tuple(
                e.model_id
                for e in (self._selection.entries if self._selection else ())
                if e.provider == choice.provider and not e.server_url
            )
            argv = choice.models_argv(stored)
            if argv[:2] == ("models", "add"):
                argv = (*argv, "--json")
            stdin = choice.key.get_secret_value() + "\n" if choice.key else None
            provider = _label(choice.provider)
            heard: list[str] = []
            results: list[dict[str, object]] = []
            total = len(choice.models)

            def hear(
                raw: str,
                results: list[dict[str, object]] = results,
                heard: list[str] = heard,
                total: int = total,
            ) -> None:
                row = _json_rows(raw)
                if row and row[0].get("model") and row[0].get("status"):
                    results.append(row[0])
                    if progress is not None:
                        progress(
                            _say(
                                "model_tested",
                                done=len(results),
                                total=total,
                                model=row[0]["model"],
                            )
                        )
                elif raw.strip():
                    heard.append(raw.strip())

            if progress is not None:
                progress(
                    _say(
                        "adding_models",
                        provider=provider,
                        models=", ".join(choice.models),
                    )
                )
            code = await self._runner.stream(argv, hear, stdin=stdin)
            if results or not code:
                lines += [_tested_line(provider, row) for row in results]
                if not results:  # add-model: no per-model results to read
                    lines.append(
                        Text(
                            f"{glyphs.done} "
                            + _say(
                                "setup_ready",
                                provider=provider,
                                models=", ".join(choice.models),
                            ),
                            style=_style("success"),
                        )
                    )
                all_ok = all_ok and all(r.get("status") == "passed" for r in results)
            else:
                all_ok = False
                refused_any = True
                lines.append(
                    Text(
                        f"{glyphs.failed} "
                        + _say(
                            "add_failed",
                            provider=provider,
                            models=", ".join(choice.models),
                            reason=plain_reason(
                                heard[-1] if heard else "", tui_design().reason_chars
                            )
                            or _say("no_answer"),
                        ),
                        style=_style("error"),
                    )
                )
        await self._refresh_status()
        # A stored model is not chosen again on a retry, even if its test
        # failed; a choice the CLI refused stays chosen for the retry.
        self.setup_ok = bool(lines) and not refused_any
        if not lines:
            return None
        outcome = Text("\n").join(lines)
        self._toast(outcome, ok=all_ok)
        return outcome

    async def load_selection(self) -> ModelTuiSelection:
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
                self._server_said(base, tuple(model for _, model in pairs))
                for base, pairs in by_server.items()
            ),
        )
        servers = {
            (provider, model, base): said[model]
            for (base, pairs), said in zip(by_server.items(), answers, strict=True)
            for provider, model in pairs
            if model in said
        }
        self._selection = selected_models(
            rows,
            [c for c in connections if isinstance(c, dict)]
            if isinstance(connections, list)
            else [],
            index,
            servers,
        )
        self._read_at = time.monotonic()
        return self._selection

    async def _server_said(
        self, base: str, models: tuple[str, ...]
    ) -> dict[str, ModelLocalServerDetails]:
        """What a server says about its models, asked again only after a while."""
        key = (base, models)
        remembered = self._said.get(key)
        keep = float(catalog_config()["local_servers"]["remember_seconds"])
        if remembered is not None and time.monotonic() - remembered[0] < keep:
            return remembered[1]
        said = await asyncio.to_thread(_safe_server, self._read_server, base, models)
        if said:  # an unreachable server is asked again next time
            self._said[key] = (time.monotonic(), said)
        return said

    async def apply_delegation(
        self, choice: ModelTuiDelegationChoice | None
    ) -> Text | str | None:
        """Run the switch's commands; the outcome goes to the log and is returned."""
        if choice is None:
            return None
        glyphs = tui_design().glyphs
        outcome: Text | str | None = None
        words = tui_design().delegation
        state = "on" if choice.on else "off"
        failed = False
        for argv in choice.commands:
            code, out, err = await self._runner.capture(argv)
            if code:
                failed = True
                reason = _delegation_fix(out, err, on=choice.on)
                outcome = Text(
                    f"{glyphs.failed} "
                    + words.not_switched.format(state=state, reason=reason),
                    style=_style("error"),
                )
                break
        else:
            done = choice.done or (
                words.switched_on if choice.on else words.switched_off
            )
            outcome = Text(f"{glyphs.done} {done}", style=_style("success"))
        await self._refresh_status()  # the open tab shows the state just read
        if outcome is not None:
            self._toast(outcome, ok=not failed)
        return outcome

    async def claude_code(self) -> tuple[str, str]:
        """Whether Claude Code is installed and signed in, from setup's own check."""
        try:
            code, out, _ = await self._runner.capture(
                tui_design().delegation.claude_check
            )
        except Exception:  # a check that cannot run never blocks a Save
            return ("unknown", "")
        # The same read says whether delegation is on: shown when it landed; a
        # failed one leaves the state as last read (review 2026-11-15).
        delegation, hooks, _ = _delegation_read(code, out)
        if delegation != "unknown":
            self._show_status(
                self._status.model_copy(
                    update={"delegation": delegation, "hooks": hooks, "problem": None}
                )
            )
        return claude_code_state(_last_json_object(out))

    async def dashboard_state(self) -> dict[str, object]:
        """`onex dashboard status --json`: the dashboard's real state."""
        _, out, _ = await self._runner.capture(tuple(tui_design().dashboard.status))
        return _last_json_object(out) or {}

    async def dashboard_action(self, name: str) -> Text:
        """Start, open or stop the dashboard; the outcome is returned and pops up."""
        words = tui_design().dashboard
        argv = {"start": words.start, "open": words.open, "stop": words.stop}[name]
        code, out, err = await self._runner.capture(tuple(argv))
        document = _last_json_object(out) or {}
        evidence = document.get("evidence")
        found = evidence if isinstance(evidence, Mapping) else {}
        ok = not code and document.get("status") == "success"
        glyphs = tui_design().glyphs
        if ok:
            done = {"start": words.started, "open": words.opened, "stop": words.stopped}
            line = Text(
                f"{glyphs.done} " + done[name].format(url=found.get("url", "")),
                style=_style("success"),
            )
        else:
            reason = document.get("fix") or _last_line(out, err)
            line = Text(
                f"{glyphs.failed} " + words.failed.format(action=name, reason=reason),
                style=_style("error"),
            )
        self._toast(line, ok=ok)
        return line

    async def remove_model(self, entry: ModelCatalogEntry) -> Text | str:
        """Remove one model through the CLI; the outcome goes to the log and is returned."""
        argv = (
            "models",
            "remove",
            entry.provider,
            "--model",
            entry.model_id,
            *(("--url", entry.server_url) if entry.server_url else ()),
        )
        code, out, err = await self._runner.capture(argv)
        glyphs = tui_design().glyphs
        named = _on_server(entry.model_id, entry.server_url)
        outcome = (
            Text(
                f"{glyphs.done} "
                + _say("removed", model=named, provider=_label(entry.provider)),
                style=_style("success"),
            )
            if not code
            else Text(
                f"{glyphs.failed} "
                + _say("remove_failed", model=named, reason=_last_line(out, err)),
                style=_style("error"),
            )
        )
        await self._refresh_status()
        self._toast(outcome, ok=not code)
        return outcome

    async def apply_model(self, picked: tuple[str, str, str]) -> Text | str:
        """Make a model first, or Auto; the outcome goes to the log and is returned."""
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
        outcome = (
            Text(f"{glyphs.done} {chosen}", style=_style("success"))
            if not code
            else Text(
                f"{glyphs.failed} " + _say("first_failed", reason=_last_line(out, err)),
                style=_style("error"),
            )
        )
        await self._refresh_status()
        self._toast(outcome, ok=not code)
        return outcome

    async def _delegate(self, task: str) -> None:
        self._write(
            Text(
                f"{tui_design().glyphs.task} {task}", style=_style("secondary", "bold")
            )
        )
        code, out, err = await self._runner.capture(("delegate", task, "--json"))
        document = _last_json_object(out)
        result = document.get("result") if document else None
        if code or not isinstance(result, dict) or not result.get("response"):
            reason = (_failure_reason(document) if document else None) or (
                err.strip().splitlines()[-1] if err.strip() else _say("no_answer")
            )
            self._write(
                Text(
                    f"{tui_design().glyphs.failed} "
                    + _say("delegation_failed", reason=reason),
                    style=_style("error"),
                )
            )
            self._write(Text(_say("exit_code", code=code), style=_style("muted")))
            return
        metrics = result.get("metrics")
        metrics = metrics if isinstance(metrics, dict) else {}
        model = str(result.get("model_name") or _say("run_unknown_model"))
        cost = _measured_cost(metrics)
        facts = [model]
        tokens_in, tokens_out = (
            metrics.get("input_tokens"),
            metrics.get("output_tokens"),
        )
        if tokens_in or tokens_out:
            facts.append(
                _say(
                    "run_tokens_in_out",
                    tokens_in=f"{int(tokens_in or 0):,}",
                    tokens_out=f"{int(tokens_out or 0):,}",
                )
            )
        elif metrics.get("total_tokens"):
            facts.append(_say("run_tokens", tokens=f"{int(metrics['total_tokens']):,}"))
        if metrics.get("latency_ms"):
            facts.append(
                _say("run_seconds", seconds=f"{int(metrics['latency_ms']) / 1000:.1f}")
            )
        facts.append(
            _say("run_cost", cost=f"{cost:.4f}")
            if cost is not None
            else _say("run_cost_unmeasured")
        )
        if cost is not None and metrics.get("cost_savings_usd"):
            facts.append(
                _say("run_saved", saved=f"{float(metrics['cost_savings_usd']):.4f}")
            )
        elif cost is None:
            facts.append(_say("run_saved_unmeasured"))
        run_id = str(document.get("run_id") or "") if document else ""
        self._main().query_one("#output", OutputPane).write(
            Markdown(str(result["response"]).strip())
        )
        self._write(
            Text(
                "  ·  ".join(facts)
                + (f"  ·  {_say('run_id', run=run_id[:8])}" if run_id else ""),
                style=_style("muted"),
            )
        )
        # The model that answered is the last run's, not the one tried first.
        self._last_run = (
            _say("last_run", model=model, cost=f"{cost:.4f}")
            if cost is not None
            else _say("last_run_unmeasured", model=model)
        ) + (f" · {_say('run_id', run=run_id[:8])}" if run_id else "")
        self._render_status()


@click.command("tui")
def tui_command() -> None:
    """Open the full-screen onex terminal UI."""
    OnexTui().run()
