# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the installed `onex` for the TUI, as a user would (OMN-23413).

The TUI is a front end only: every palette command and every delegation runs
the same `onex` command the CLI runs, through this handler. A cancelled run
stops its process. Bound in this node's contract under cli_served_handlers.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


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
