# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run a call in a worker process the caller terminates at a deadline.

A thread join leaves a hung call running; a process can be ended. The caller waits on the worker's
pipe for at most the deadline, and on expiry terminates the worker (then kills it if it ignores the
signal), reaps it and closes every descriptor it opened, so a run of hangs leaves no live worker and
no open socket behind.
"""

from __future__ import annotations

import multiprocessing
import time
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Literal

#: Seconds the caller waits for a terminated worker to exit before it kills it.
TERMINATE_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class WorkerOutcome[T]:
    status: Literal["ok", "raised", "deadline"]
    elapsed_seconds: float
    value: T | None = None
    #: The exception's class name and text when the call raised in the worker.
    error_type: str | None = None
    error_text: str | None = None
    #: A ``reason`` attribute's value on the exception (a typed transport reason), when it had one.
    error_reason: str | None = None


def _child(conn: Connection, target: Callable[[], object]) -> None:
    try:
        conn.send(("ok", target()))
    except BaseException as exc:  # the worker reports every failure; the caller decides
        reason = getattr(getattr(exc, "reason", None), "value", None)
        conn.send(("raised", type(exc).__name__, str(exc), reason))
    finally:
        conn.close()


def run_with_deadline[T](
    target: Callable[[], T], deadline_seconds: float
) -> WorkerOutcome[T]:
    """Run ``target`` in a worker; its value, its exception, or a deadline expiry (the worker ended)."""
    context = multiprocessing.get_context("fork")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(writer, target), daemon=True)
    started = time.monotonic()
    try:
        process.start()
        # The parent's copy of the write end closes now, so a worker that dies is seen as end of file.
        writer.close()
        if not reader.poll(deadline_seconds):
            return WorkerOutcome(
                status="deadline", elapsed_seconds=time.monotonic() - started
            )
        try:
            message = reader.recv()
        except EOFError:
            return WorkerOutcome(
                status="raised",
                elapsed_seconds=time.monotonic() - started,
                error_type="WorkerExited",
                error_text="the worker ended without an answer",
            )
        elapsed = time.monotonic() - started
        if message[0] == "ok":
            return WorkerOutcome(status="ok", elapsed_seconds=elapsed, value=message[1])
        return WorkerOutcome(
            status="raised",
            elapsed_seconds=elapsed,
            error_type=message[1],
            error_text=message[2],
            error_reason=message[3],
        )
    finally:
        _end(process)
        reader.close()
        if not writer.closed:
            writer.close()


def _end(process: multiprocessing.process.BaseProcess) -> None:
    """Terminate, then kill, then reap the worker and release its handles."""
    if process.pid is None:
        return
    if process.is_alive():
        process.terminate()
        process.join(TERMINATE_GRACE_SECONDS)
    if process.is_alive():
        process.kill()
    process.join()
    process.close()
