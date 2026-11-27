# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the installed `onex` for the TUI, as a user would (OMN-23413).

The TUI is a front end only: every palette command and every delegation runs
the same `onex` command the CLI runs, through this handler. A cancelled run
stops its process. Bound in this node's contract under cli_served_handlers.

OMN-26611: starting a new `onex` program cost 2.3 to 3.2 s before any work, so a
short read or setup command (the paths listed under ``in_process`` in
tui_design.yaml) runs inside the running TUI instead: the same click command
tree is invoked in a worker thread with its output captured and ``SystemExit``
caught. A command that waits on a model or a network, serves, prompts, or is
fed text on stdin keeps the subprocess path, so cancelling it still ends its
whole process group.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import signal
import subprocess
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import TextIO, cast

import click

from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

#: The capture a worker thread's output goes to, by stream name; a thread with
#: none writes to the real stream.
_ROUTE = threading.local()
_ROUTE_LOCK = threading.Lock()


class _RouteState:
    """How many in-process commands run, and the streams to put back after."""

    users = 0
    out: TextIO | None = None
    err: TextIO | None = None


class _Routed:
    """Stands in for sys.stdout or sys.stderr while in-process commands run: a
    worker thread running one writes to its own capture, any other thread to the
    stream that was there."""

    def __init__(self, name: str, real: TextIO) -> None:
        self._name = name
        self._real = real

    def _target(self) -> TextIO:
        captured: TextIO | None = getattr(_ROUTE, self._name, None)
        return captured or self._real

    def write(self, text: str) -> int:
        return self._target().write(text)

    def flush(self) -> None:
        self._target().flush()

    def isatty(self) -> bool:
        return self._target().isatty()

    def __getattr__(self, name: str) -> object:
        return getattr(self._target(), name)


def _route_in() -> None:
    with _ROUTE_LOCK:
        if _RouteState.users == 0:
            _RouteState.out, _RouteState.err = sys.stdout, sys.stderr
            sys.stdout = cast(TextIO, _Routed("out", sys.stdout))
            sys.stderr = cast(TextIO, _Routed("err", sys.stderr))
        _RouteState.users += 1


def _route_out() -> None:
    with _ROUTE_LOCK:
        _RouteState.users -= 1
        if _RouteState.users == 0 and _RouteState.out and _RouteState.err:
            sys.stdout, sys.stderr = _RouteState.out, _RouteState.err


def _exit_code(code: object) -> int:
    if code is None:
        return 0
    return code if isinstance(code, int) else 1


def _stop(process: asyncio.subprocess.Process) -> None:
    """End a cancelled onex run and every process it started, so none keeps working.

    OMN-23413: killing only the onex process left `onex delegate`'s
    multiprocessing worker and resource tracker running under PID 1, so a
    cancelled task could go on calling the model. Each run is started in its
    own session (start_new_session=True), so its pid is its process group and
    the whole group is ended. Drafted by onex delegate run b201a7e0.
    """
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()


class HandlerOnexCommand:
    """Runs the installed `onex` beside this interpreter, as a user would."""

    def __init__(self, root: click.Group | None = None) -> None:
        self._onex = str(Path(sys.executable).parent / "onex")
        self._root = root

    def _runs_here(self, argv: tuple[str, ...], stdin: str | None) -> bool:
        """Whether this command runs inside the TUI (see the module note)."""
        return stdin is None and any(
            argv[: len(path)] == path for path in tui_design().in_process
        )

    def _invoke(self, argv: tuple[str, ...], out: io.StringIO, err: io.StringIO) -> int:
        """Run one onex command here, in the calling worker thread, as `onex`
        would: its output goes to ``out`` and ``err``, its exit to the return."""
        _route_in()
        _ROUTE.out, _ROUTE.err = out, err
        try:
            root = self._root
            if root is None:
                from omnibase_core.cli.cli_commands import cli

                root = cli
            try:
                with root.make_context("onex", list(argv)) as ctx:
                    root.invoke(ctx)
                return 0
            except click.exceptions.Exit as done:
                return done.exit_code
            except click.Abort:
                err.write("Aborted!\n")
                return 1
            except click.ClickException as error:
                err.write(f"Error: {error.format_message()}\n")
                return error.exit_code
            except SystemExit as leave:
                return _exit_code(leave.code)
            except Exception:
                err.write(traceback.format_exc())
                return 1
        finally:
            _ROUTE.out = _ROUTE.err = None
            _route_out()

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        if self._runs_here(argv, stdin):
            merged = io.StringIO()  # one stream, as the subprocess merges stderr
            code = await asyncio.to_thread(self._invoke, argv, merged, merged)
            for line in merged.getvalue().splitlines():
                emit(line)
            return code
        process = await asyncio.create_subprocess_exec(
            self._onex,
            *argv,
            stdin=asyncio.subprocess.DEVNULL
            if stdin is None
            else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
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
        if self._runs_here(argv, stdin):
            said, complained = io.StringIO(), io.StringIO()
            code = await asyncio.to_thread(self._invoke, argv, said, complained)
            return code, said.getvalue(), complained.getvalue()
        process = await asyncio.create_subprocess_exec(
            self._onex,
            *argv,
            stdin=asyncio.subprocess.DEVNULL
            if stdin is None
            else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
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
