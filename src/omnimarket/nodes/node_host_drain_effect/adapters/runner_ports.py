# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real ports of the runner stop and the lane count: a process-table probe, an argv stop and an argv lane count."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumRunnerActivity,
)
from omnimarket.nodes.node_host_drain_effect.overlay import (
    ModelLaneCountSettings,
    ModelRunnerStopSettings,
)

CommandRun = Callable[..., "subprocess.CompletedProcess[str]"]


class ProcRunnerProbe:
    """A runner holds a job exactly when a worker process runs from its directory.

    Reads the process table under ``proc_root``. A table that cannot be listed gives UNKNOWN, and so does a runner the
    overlay does not declare: the stop treats both as a refusal, never as idle.
    """

    def __init__(
        self,
        settings: ModelRunnerStopSettings,
        *,
        proc_root: Path | None = None,
        runner_dirs: Mapping[str, str] | None = None,
    ) -> None:
        self._worker = settings.worker_process_name
        self._listener = settings.listener_process_name
        self._proc = proc_root or Path(settings.proc_root)
        self._dirs = dict(
            runner_dirs or {r.name: r.directory for r in settings.runners}
        )

    def _names_running(self, runner_name: str) -> set[str] | None:
        """The runner-process names running from the runner's directory; None when it cannot be read."""
        directory = self._dirs.get(runner_name)
        if directory is None:
            return None
        prefix = directory.rstrip("/") + "/"
        try:
            entries = [p for p in self._proc.iterdir() if p.name.isdigit()]
        except OSError:
            return None
        found: set[str] = set()
        for entry in entries:
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            argv0 = raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")
            if argv0.startswith(prefix):
                found.add(Path(argv0).name)
        return found

    def activity(self, runner_name: str) -> EnumRunnerActivity:
        names = self._names_running(runner_name)
        if names is None:
            return EnumRunnerActivity.UNKNOWN
        return (
            EnumRunnerActivity.BUSY
            if self._worker in names
            else EnumRunnerActivity.IDLE
        )

    def is_running(self, runner_name: str) -> bool | None:
        names = self._names_running(runner_name)
        if names is None:
            return None
        return bool(names & {self._worker, self._listener})


class CommandRunnerProbe:
    """Reads a runner by the overlay's argv (a container runner): busy, idle or unknown, and whether it is up."""

    def __init__(
        self, settings: ModelRunnerStopSettings, *, run: CommandRun = subprocess.run
    ) -> None:
        self._settings = settings
        self._probe = settings.command_probe
        self._run = run

    def _output(self, argv_template: tuple[str, ...], runner_name: str) -> str | None:
        row = next((r for r in self._settings.runners if r.name == runner_name), None)
        if row is None:
            return None
        argv = [part.replace("{unit}", row.unit) for part in argv_template]
        try:
            done = self._run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._probe.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    def activity(self, runner_name: str) -> EnumRunnerActivity:
        out = self._output(self._probe.activity_command, runner_name)
        if out == self._probe.busy_token:
            return EnumRunnerActivity.BUSY
        if out == self._probe.idle_token:
            return EnumRunnerActivity.IDLE
        return EnumRunnerActivity.UNKNOWN

    def is_running(self, runner_name: str) -> bool | None:
        out = self._output(self._probe.running_command, runner_name)
        if out is None:
            return None
        return out == self._probe.running_token


class CommandRunnerStopper:
    """Stops a runner's service with the overlay's argv; no shell."""

    def __init__(
        self, settings: ModelRunnerStopSettings, *, run: CommandRun = subprocess.run
    ) -> None:
        self._settings = settings
        self._run = run

    def stop(self, runner_name: str) -> bool:
        row = next((r for r in self._settings.runners if r.name == runner_name), None)
        if row is None:
            return False
        argv = [
            part.replace("{unit}", row.unit) for part in self._settings.stop_command
        ]
        try:
            done = self._run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._settings.stop_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0


class CommandLaneSource:
    """Counts the lanes on a host with the overlay's argv, which prints one integer."""

    def __init__(
        self, settings: ModelLaneCountSettings, *, run: CommandRun = subprocess.run
    ) -> None:
        self._settings = settings
        self._run = run

    def running(self, host_name: str) -> int:
        argv = [
            part.replace("{host}", host_name) for part in self._settings.count_command
        ]
        done = self._run(
            argv,
            capture_output=True,
            text=True,
            timeout=self._settings.timeout_seconds,
            check=True,
        )
        return int(done.stdout.strip())
