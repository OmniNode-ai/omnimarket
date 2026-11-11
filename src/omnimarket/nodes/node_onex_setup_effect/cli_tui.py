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
from pydantic import BaseModel, ConfigDict
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

_PROMPTING_OPTIONS = frozenset(("interactive", "yes"))
_LOCAL_COMMANDS = {
    ("help",): "Show keys and how commands work.",
    ("quit",): "Leave the TUI.",
}


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


class ProtocolTuiRunner(Protocol):
    async def stream(
        self, argv: tuple[str, ...], emit: Callable[[str], None]
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


class OnexProcessRunner:
    """Runs the installed `onex` beside this interpreter, as a user would."""

    def __init__(self) -> None:
        self._onex = str(Path(sys.executable).parent / "onex")

    async def stream(self, argv: tuple[str, ...], emit: Callable[[str], None]) -> int:
        process = await asyncio.create_subprocess_exec(
            self._onex,
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None
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
    runner = OnexProcessRunner()
    model: str | None = None
    gateway = False
    models: list[str] = []
    await runner.stream(("models", "list", "--json"), models.append)
    for line in models:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("set_up") and row.get("first_model"):
            model = str(row["first_model"])
            if row.get("tried_first"):
                break
    claude: list[str] = []
    await runner.stream(("claude", "status", "--json"), claude.append)
    for line in claude:
        try:
            gateway = bool(json.loads(line).get("enabled"))
        except ValueError:
            continue
    return ModelTuiStatus(workspace=str(Path.cwd()), model=model, gateway=gateway)


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
    ) -> None:
        super().__init__()
        if root is None:
            from omnibase_core.cli.cli_commands import cli

            root = cli
        self._commands = command_tree(root)
        self._runner: ProtocolTuiRunner = runner or OnexProcessRunner()
        self._load_status = load_status or globals()["load_status"]
        self._status = ModelTuiStatus(workspace=str(Path.cwd()))
        self._last_run = ""

    def compose(self) -> ComposeResult:
        yield RichLog(id="output", wrap=True, markup=False, highlight=False)
        yield OptionList(id="palette")
        yield Input(
            id="prompt",
            placeholder="Ask ONEX to delegate a task, or type / for commands",
        )
        yield Static(id="status")

    async def on_mount(self) -> None:
        self._write(
            "Welcome to onex. Type a task to delegate it, or / to see every command."
        )
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
            for entry in (*self._commands, *local)
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

    def action_palette_down(self) -> None:
        palette = self.query_one("#palette", OptionList)
        if palette.display:
            palette.action_cursor_down()

    def action_palette_up(self) -> None:
        palette = self.query_one("#palette", OptionList)
        if palette.display:
            palette.action_cursor_up()

    def action_complete(self) -> None:
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
                "Type a task and press Enter to delegate it. Type / to list every onex "
                "command; keep typing to filter, Tab to complete, Enter to run, Esc to "
                "close. Commands that ask questions open in the terminal and return here."
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
        self._write(str(result["response"]).strip())
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
