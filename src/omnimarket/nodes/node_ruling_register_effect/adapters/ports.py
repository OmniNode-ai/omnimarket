# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read ports and event sinks of the ruling register.

The in-memory port returns the readings a fixture world holds; a row without a fixture
reads UNREAD. The command port runs each row's read through its route -- a local host's
argv directly, a remote host's argv through the route's prefix with the host's target
substituted and the argv shlex-joined as one final argument. A read never raises: a host
with no target, a command that did not run, timed out or exited with a code the row does
not accept all read UNREAD, and UNREAD is never TRUE. Nothing about a host or a target is
in this file; the route is.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.model_ruling_register import (
    EnumReadStatus,
    ModelRulingReading,
    ModelRulingRow,
)
from omnimarket.nodes.node_ruling_register_effect.models import (
    ModelRulingReadEvent,
    ModelRulingRoute,
)
from omnimarket.nodes.node_ruling_register_effect.models.model_ruling_register_effect import (
    TARGET_TOKEN,
)

#: The runner of a command read: the argv and the route's timeout to its exit code and stdout.
Runner = Callable[[list[str], int], tuple[int, str]]

#: How much of a refused read's stdout is kept in its reading.
_STDOUT_LIMIT = 2000


def run_argv(argv: list[str], timeout_seconds: int) -> tuple[int, str]:
    """Run the argv and return its exit code and stdout; a timeout raises ``TimeoutExpired``."""
    done = subprocess.run(
        argv, capture_output=True, text=True, check=False, timeout=timeout_seconds
    )
    return done.returncode, done.stdout


class InMemoryRulingReadPort:
    """The readings a test or a fixture world holds; a row without a fixture reads UNREAD."""

    def __init__(
        self,
        readings: Mapping[str, ModelRulingReading],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._readings = readings
        self._clock = clock

    def read(self, row: ModelRulingRow) -> ModelRulingReading:
        reading = self._readings.get(row.row_id)
        if reading is None:
            return ModelRulingReading(
                row_id=row.row_id,
                status=EnumReadStatus.UNREAD,
                read_at=self._clock(),
                detail="no reading fixture",
            )
        return reading


class CommandRulingReadPort:
    """Each row's read through its route: a local host's argv directly, a remote host's
    argv through the route's prefix with the host's target substituted."""

    def __init__(
        self,
        route: ModelRulingRoute,
        runner: Runner = run_argv,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._route = route
        self._runner = runner
        self._clock = clock

    def read(self, row: ModelRulingRow) -> ModelRulingReading:
        host = row.read.host
        if host in self._route.local_hosts:
            argv = list(row.read.argv)
        elif (target := self._route.host_targets.get(host)) is None:
            return self._unread(row, detail=f"no target for host {host}")
        else:
            prefix = [
                token.replace(TARGET_TOKEN, target)
                for token in self._route.remote_argv_prefix
            ]
            argv = [*prefix, shlex.join(list(row.read.argv))]
        try:
            exit_code, stdout = self._runner(argv, self._route.timeout_seconds)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._unread(row, detail=type(exc).__name__)
        if exit_code not in row.read.ok_exit_codes:
            return self._unread(
                row,
                exit_code=exit_code,
                stdout=stdout[:_STDOUT_LIMIT],
                detail=f"exit {exit_code}",
            )
        return ModelRulingReading(
            row_id=row.row_id,
            status=EnumReadStatus.OK,
            exit_code=exit_code,
            stdout=stdout,
            read_at=self._clock(),
        )

    def _unread(
        self,
        row: ModelRulingRow,
        *,
        detail: str,
        exit_code: int | None = None,
        stdout: str = "",
    ) -> ModelRulingReading:
        return ModelRulingReading(
            row_id=row.row_id,
            status=EnumReadStatus.UNREAD,
            exit_code=exit_code,
            stdout=stdout,
            read_at=self._clock(),
            detail=detail,
        )


class InMemoryRulingSink:
    """Keeps the events in a list; the default and the tests' sink."""

    def __init__(self) -> None:
        self.events: list[ModelRulingReadEvent] = []

    def emit(self, event: ModelRulingReadEvent) -> None:
        self.events.append(event)


class JournalRulingSink:
    """Appends each event as one JSON line; a host that keeps a journal reads it back."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def emit(self, event: ModelRulingReadEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")


__all__ = [
    "CommandRulingReadPort",
    "InMemoryRulingReadPort",
    "InMemoryRulingSink",
    "JournalRulingSink",
    "Runner",
    "run_argv",
]
