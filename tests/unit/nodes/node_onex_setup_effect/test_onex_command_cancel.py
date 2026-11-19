# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Esc ends a running onex task and everything it started (OMN-23413).

Measured 2026-11-13 on the real CLI: SIGKILL of a running `onex delegate`
left its two children alive under PID 1, a multiprocessing resource tracker
and a `spawn_main` worker, so a cancelled task could go on calling the model.

Failure modes:
  C1  cancelling a captured run leaves a process it started running.
  C2  cancelling a streamed run leaves a process it started running.
  C3  (control) a run that finished on its own is not signalled again.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_command import (
    HandlerOnexCommand,
)

pytestmark = pytest.mark.unit


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _runner_with_child(tmp_path: Path) -> tuple[HandlerOnexCommand, Path]:
    """An `onex` that starts a long-lived child, records its pid, and waits."""
    pid_file = tmp_path / "child.pid"
    onex = tmp_path / "onex"
    onex.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > {pid_file}\nwait\n")
    onex.chmod(0o755)
    runner = HandlerOnexCommand()
    runner._onex = str(onex)  # the fake, in place of the installed onex
    return runner, pid_file


async def _cancel_once_started(
    start: Callable[[], Awaitable[object]], pid_file: Path
) -> int:
    task = asyncio.create_task(start())
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.05)
    child = int(pid_file.read_text())
    assert _alive(child)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(40):
        if not _alive(child):
            break
        await asyncio.sleep(0.05)
    return child


async def test_cancelling_a_captured_run_ends_what_it_started(tmp_path: Path) -> None:
    """C1."""
    runner, pid_file = _runner_with_child(tmp_path)
    child = await _cancel_once_started(
        lambda: runner.capture(("delegate", "x")), pid_file
    )
    assert not _alive(child)


async def test_cancelling_a_streamed_run_ends_what_it_started(tmp_path: Path) -> None:
    """C2."""
    runner, pid_file = _runner_with_child(tmp_path)
    child = await _cancel_once_started(
        lambda: runner.stream(("models", "add"), lambda _line: None), pid_file
    )
    assert not _alive(child)


async def test_a_finished_run_answers_normally(tmp_path: Path) -> None:
    """C3."""
    onex = tmp_path / "onex"
    onex.write_text('#!/bin/sh\necho "$@"\n')
    onex.chmod(0o755)
    runner = HandlerOnexCommand()
    runner._onex = str(onex)
    code, out, _err = await runner.capture(("delegate", "hi"))
    assert (code, out.strip()) == (0, "delegate hi")
