# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime backend: the lane's inbox read through the overlay's reader module, and real time."""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable, Mapping
from typing import Any

from omnimarket.nodes.node_lane_inbox_wait_effect.overlay import ModelReaderSettings
from omnimarket.nodes.node_lane_inbox_wait_effect.protocols import InboxUnreadableError


class ReaderInboxSource:
    """One read per poll: the reader module run as a subprocess over the inbox window.

    The interpreter command comes from the environment variable the overlay names
    (its first token is the executable); the reader answers the window's ledger rows on
    stdout, one row per line. Anything else - a missing variable, a value that does not
    split, a non-ok exit, an OSError, a timeout - is :class:`InboxUnreadableError`
    naming why, never an empty read.
    """

    def __init__(
        self,
        reader: ModelReaderSettings,
        environ: Mapping[str, str],
        run: Callable[..., Any] = subprocess.run,
    ) -> None:
        self._reader = reader
        self._environ = environ
        self._run = run

    def read(self, *, lane: str) -> tuple[str, ...]:
        reader = self._reader
        raw = self._environ.get(reader.interpreter_env, "").strip()
        try:
            argv0 = shlex.split(raw)[0] if raw else ""
        except ValueError as exc:
            raise InboxUnreadableError(
                f"{reader.interpreter_env} does not split into a command ({exc})"
            ) from exc
        if not argv0:
            raise InboxUnreadableError(
                f"{reader.interpreter_env} is not set; it names the interpreter "
                "command the inbox reader runs under"
            )
        argv = [
            argv0,
            "-m",
            reader.reader_module,
            "--reader",
            reader.reader_name,
            "--query",
            "rows",
            "--window-hours",
            str(reader.window_hours),
            "--lane",
            lane,
        ]
        try:
            done = self._run(
                argv,
                capture_output=True,
                text=True,
                timeout=reader.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise InboxUnreadableError(
                f"{reader.unreadable_label}: the inbox reader timed out after "
                f"{reader.timeout_seconds:g}s"
            ) from exc
        except OSError as exc:
            raise InboxUnreadableError(
                f"{reader.unreadable_label}: the inbox reader could not run "
                f"({type(exc).__name__})"
            ) from exc
        if done.returncode not in reader.ok_exits:
            raise InboxUnreadableError(
                f"{reader.unreadable_label}: the inbox reader exited {done.returncode}"
            )
        return tuple(line for line in done.stdout.splitlines() if line.strip())


class TimeSleeper:
    """Waits out one poll interval on the real clock."""

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class MonotonicClock:
    """The process monotonic clock, the only clock the wait's budget is measured on."""

    def monotonic(self) -> float:
        return time.monotonic()


__all__ = ["MonotonicClock", "ReaderInboxSource", "TimeSleeper"]
