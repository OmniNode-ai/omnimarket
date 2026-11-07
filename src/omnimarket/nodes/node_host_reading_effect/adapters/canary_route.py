# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canary ports of node_host_reading_effect: in memory for tests, the real route and table for the CLI.

The real route is the command the overlay names (the placement lanes take, its pool read and its
lane wrapper); the real table is the lab host table the overlay store serves. Neither names a
host, a path or a repository in code.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence

from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_host_reading import ModelCanaryHost, ModelCanaryRun


class SubprocessCanaryRoute:
    """Run the route as a child process group; a deadline kills the group and reads timed_out."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = dict(os.environ if environ is None else environ)

    def run(self, argv: Sequence[str], timeout_seconds: int) -> ModelCanaryRun:
        process = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env=self._environ,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            return ModelCanaryRun(
                exit_code=None,
                stdout=stdout or "",
                stderr=stderr or "",
                timed_out=True,
            )
        return ModelCanaryRun(
            exit_code=process.returncode, stdout=stdout or "", stderr=stderr or ""
        )


class InMemoryCanaryRoute:
    """Answer each canary from a function of the host the route names; records every call."""

    def __init__(
        self,
        answer: Callable[[str, Sequence[str]], ModelCanaryRun],
        host_of: Callable[[Sequence[str]], str],
    ) -> None:
        self._answer = answer
        self._host_of = host_of
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], timeout_seconds: int) -> ModelCanaryRun:
        self.calls.append(tuple(argv))
        return self._answer(self._host_of(argv), argv)


class HostTableInventory:
    """The lab host table from the overlay store (pinned file, overlay roots, then workspace)."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def hosts(self) -> tuple[ModelCanaryHost, ...]:
        table = resolve_lab_hosts_table(self._environ)
        return tuple(
            ModelCanaryHost(
                name=row.name,
                pooled=row.available,
                reason="" if row.available else (row.unavailable_reason or "parked"),
            )
            for row in table.hosts
        )


class InMemoryHostInventory:
    """A fixed host table; ``error`` makes the read fail the way an unreadable table does."""

    def __init__(
        self, hosts: Sequence[ModelCanaryHost], error: str | None = None
    ) -> None:
        self._hosts = tuple(hosts)
        self.error = error

    def hosts(self) -> tuple[ModelCanaryHost, ...]:
        if self.error is not None:
            raise ValueError(self.error)
        return self._hosts


class CommandAdvertisementSource:
    """The advertisements a configured command prints as one JSON list."""

    def __init__(
        self, command: str, timeout_seconds: int, environ: Mapping[str, str]
    ) -> None:
        self._argv = shlex.split(command)
        self._timeout = timeout_seconds
        self._environ = dict(environ)

    def read(self) -> tuple[ModelHostCapacityAdvertisement, ...]:
        if not self._argv:
            raise ValueError("advertisement command is empty")
        try:
            done = subprocess.run(
                self._argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=self._environ,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError(
                f"advertisement read timed out after {self._timeout}s"
            ) from exc
        if done.returncode != 0:
            raise OSError(f"advertisement read exited {done.returncode}")
        start = done.stdout.find("[")
        if start < 0:
            raise ValueError("advertisement read printed no JSON list")
        return tuple(
            ModelHostCapacityAdvertisement.model_validate(item)
            for item in json.loads(done.stdout[start:])
        )


class InMemoryAdvertisementSource:
    """A fixed list of advertisements; ``error`` makes the read fail."""

    def __init__(
        self,
        adverts: Sequence[ModelHostCapacityAdvertisement],
        error: str | None = None,
    ) -> None:
        self.adverts = list(adverts)
        self.error = error

    def read(self) -> tuple[ModelHostCapacityAdvertisement, ...]:
        if self.error is not None:
            raise OSError(self.error)
        return tuple(self.adverts)


__all__ = [
    "CommandAdvertisementSource",
    "HostTableInventory",
    "InMemoryAdvertisementSource",
    "InMemoryCanaryRoute",
    "InMemoryHostInventory",
    "SubprocessCanaryRoute",
]
