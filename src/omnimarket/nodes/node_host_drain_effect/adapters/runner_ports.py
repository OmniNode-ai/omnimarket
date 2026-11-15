# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real ports of the runner stop and the lane count: a process-table probe, an argv stop, an argv lane count and a receipts lane count."""

from __future__ import annotations

import json
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


class ProcWorkerProbe:
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
            except (FileNotFoundError, ProcessLookupError):
                # The process exited between the listing and the read: it holds no job.
                continue
            except OSError:
                # A process that is there but cannot be read may be a live worker: stay uncertain.
                return None
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


class CommandWorkerProbe:
    """Reads a runner by the overlay's argv (a container runner): busy, idle or unknown, and whether it is up.

    Only the declared running and stopped tokens establish a state; any other output reads unknown (None).
    """

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
        if out == self._probe.running_token:
            return True
        if out == self._probe.stopped_token:
            return False
        # No output, a failed command or an unrecognized status establishes neither state.
        return None


class CommandWorkerStopper:
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
        if self._settings.count_command is None:
            raise ValueError("the lanes block declares no count_command")
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


class ReceiptLaneSource:
    """Counts a host's running lanes from the remote-lane runner's own receipts.

    Reads ``<directory>/<lane>/*.json``. A file is a lane receipt only when it parses to a dict carrying the key
    ``run_id``; a dict without ``run_id`` is not a lane receipt and is ignored. A receipt that cannot be read raises
    OSError and one that does not parse as JSON raises ValueError, each naming the file path: an unreadable count is
    never a measured zero. A lane receipt counts when it is not final and it names the host, read from ``host`` or,
    failing that, from ``prerequisites.host``; a non-final receipt that names no host raises ValueError so the count
    reads unreadable. Status and the launcher ``pid`` no longer matter: a non-final receipt counts whatever its
    status and even when its launcher pid is dead.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @staticmethod
    def _host_of(data: Mapping[str, object]) -> str | None:
        """The host a receipt names: ``host`` when a non-empty string, else ``prerequisites.host`` when it is."""
        host = data.get("host")
        if isinstance(host, str) and host:
            return host
        prerequisites = data.get("prerequisites")
        if isinstance(prerequisites, Mapping):
            nested = prerequisites.get("host")
            if isinstance(nested, str) and nested:
                return nested
        return None

    def running(self, host_name: str) -> int:
        # The listing itself is the read: a missing or unreadable directory raises, it never reads as zero.
        lane_dirs = [entry for entry in self._directory.iterdir() if entry.is_dir()]
        count = 0
        for lane_dir in lane_dirs:
            for receipt in lane_dir.glob("*.json"):
                try:
                    text = receipt.read_text(encoding="utf-8")
                except OSError as exc:
                    # A receipt that cannot be read holds no evidence of a lane: the count reads unreadable.
                    raise OSError(f"cannot read lane receipt: {receipt}") from exc
                try:
                    data = json.loads(text)
                except ValueError as exc:
                    raise ValueError(
                        f"lane receipt does not parse as JSON: {receipt}"
                    ) from exc
                if not isinstance(data, dict) or "run_id" not in data:
                    # A file that is not a lane receipt holds no evidence of a lane: it is ignored.
                    continue
                if data.get("final") is True:
                    # A final receipt is done; it never counts, whatever its host.
                    continue
                host = self._host_of(data)
                if host is None:
                    # A non-final receipt that names no host cannot be attributed: read it as unreadable.
                    raise ValueError(f"lane receipt names no host: {receipt}")
                if host == host_name:
                    count += 1
        return count
