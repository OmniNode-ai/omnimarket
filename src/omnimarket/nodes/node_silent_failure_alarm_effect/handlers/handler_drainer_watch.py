# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the hook-emit drainer's job, interpreter and status, and reinstall it (OMN-19998).

The observations feed the drainer decision of the silent-failure alarm compute node; the
reinstall is the installer's own script run with ONEX_HOOK_EMIT_PRESERVE_BACKLOG=1, so a backlog
the dead drainer left is published and never evicted. Every external command is run through one
injected callable, so a test states what the supervisor said instead of producing a machine state.
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from omnimarket.models.silent_failure_alarm import (
    ModelDrainerObservation,
    ModelDrainerRepair,
)

from ..models import ModelDrainerWatchOptions
from .handler_alarm_io import one_line

STATUS_FILENAME = "hook_emit_drainer_status.json"
JOURNAL_DIRNAME = "hook_emit_journal"

JobState = Literal["running", "not-running", "unknown"]
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _epoch(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


class HandlerDrainerWatch:
    """Observe the drainer on this host and reinstall it when the decision says to."""

    def __init__(
        self,
        options: ModelDrainerWatchOptions,
        *,
        run: Runner = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._options = options
        self._run = run
        self._sleep = sleep

    def observe(self) -> ModelDrainerObservation:
        """One read of the supervisor job, the interpreter it names and the status file."""
        options = self._options
        supervisor = self._supervisor()
        if supervisor == "launchd":
            state, detail = self._launchd_state()
            interpreter = self._launchd_interpreter()
        else:
            state, detail = self._systemd_state()
            interpreter = self._systemd_interpreter()
        status = self._read_status(options.state_dir / STATUS_FILENAME)
        return ModelDrainerObservation(
            job_state=state,
            job_detail=detail,
            interpreter_path=None
            if interpreter is None
            else self._display(interpreter),
            interpreter_present=None
            if interpreter is None
            else self._executable(interpreter),
            status_readable=status is not None,
            last_cycle_at=None if status is None else status[0],
            last_publish_at=None if status is None else status[1],
            journal_depth=self._journal_depth(options.state_dir / JOURNAL_DIRNAME),
        )

    def repair(self) -> ModelDrainerRepair:
        """Run the installer with the backlog preserved and read the drainer again."""
        options = self._options
        env: Mapping[str, str] = {
            **os.environ,
            "ONEX_HOOK_EMIT_PRESERVE_BACKLOG": "1",
            "OMNI_HOME": str(options.registry_root),
            "ONEX_STATE_DIR": str(options.state_dir),
        }
        if not options.installer.is_file():
            return ModelDrainerRepair(
                succeeded=False,
                detail=f"installer not found: {options.installer.name}",
                after=self.observe(),
            )
        try:
            preview = self._run(
                ["bash", str(options.installer), "--dry-run"],
                capture_output=True,
                text=True,
                check=False,
                timeout=options.repair_timeout_seconds,
                env=env,
                cwd=options.registry_root,
            )
            if preview.returncode != 0 or not self._preserves_capture(preview.stdout):
                return ModelDrainerRepair(
                    succeeded=False,
                    detail="installer preview does not preserve backlog and the observed state root; activation refused",
                    after=self.observe(),
                )
            done = self._run(
                ["bash", str(options.installer)],
                capture_output=True,
                text=True,
                check=False,
                timeout=options.repair_timeout_seconds,
                env=env,
                cwd=options.registry_root,
            )
            ok = done.returncode == 0
            tail = one_line((done.stderr or done.stdout).strip()[-200:], "no output")
            detail = f"installer exited {done.returncode}: {tail}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok, detail = False, one_line(exc, "the installer could not run")
        after = self.observe()
        for _ in range(options.settle_seconds):
            if not ok or after.job_state == "running":
                break
            self._sleep(1)
            after = self.observe()
        return ModelDrainerRepair(succeeded=ok, detail=detail, after=after)

    def _preserves_capture(self, preview: str) -> bool:
        """Check the rendered job rather than assuming the installer honors its environment."""
        if self._supervisor() == "launchd":
            try:
                payload = plistlib.loads(preview[preview.index("<?xml") :].encode())
                return "--preserve-backlog" in payload["ProgramArguments"] and payload[
                    "EnvironmentVariables"
                ]["ONEX_STATE_DIR"] == str(self._options.state_dir)
            except (ValueError, KeyError, TypeError, plistlib.InvalidFileException):
                return False
        try:
            lines = preview.splitlines()
            commands = [
                shlex.split(line.removeprefix("ExecStart="))
                for line in lines
                if line.startswith("ExecStart=")
            ]
            environments = [
                shlex.split(line.removeprefix("Environment="))
                for line in lines
                if line.startswith("Environment=")
            ]
            return (
                len(commands) == 1
                and "--preserve-backlog" in commands[0]
                and f"ONEX_STATE_DIR={self._options.state_dir}"
                in [word for values in environments for word in values]
            )
        except ValueError:
            return False

    def _supervisor(self) -> str:
        chosen = self._options.supervisor
        if chosen != "auto":
            return chosen
        return "launchd" if shutil.which("launchctl") else "systemd"

    def _probe(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return self._run(
                list(argv),
                capture_output=True,
                text=True,
                check=False,
                timeout=self._options.probe_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _launchd_state(self) -> tuple[JobState, str]:
        options = self._options
        uid = options.uid if options.uid is not None else os.getuid()
        done = self._probe(["launchctl", "print", f"gui/{uid}/{options.label}"])
        if done is None:
            return "unknown", "launchctl could not be run"
        if done.returncode != 0:
            tail = one_line(done.stderr.strip()[-120:], "no stderr")
            return (
                "not-running",
                f"not loaded (launchctl exit {done.returncode}: {tail})",
            )
        fields: dict[str, str] = {}
        for line in done.stdout.splitlines():
            key, sep, value = line.strip().partition(" = ")
            if sep and key in ("state", "last exit code"):
                fields.setdefault(key, value.strip())
        state = fields.get("state", "")
        detail = f"state {state or 'unreported'}, last exit code {fields.get('last exit code', '?')}"
        return ("running" if state == "running" else "not-running"), detail

    def _systemd_state(self) -> tuple[JobState, str]:
        unit = f"{self._options.label}.service"
        done = self._probe(["systemctl", "--user", "is-active", unit])
        if done is None:
            return "unknown", "systemctl could not be run"
        word = done.stdout.strip()
        if word == "active":
            return "running", "active"
        if word in ("inactive", "failed", "activating", "deactivating", "reloading"):
            return "not-running", word
        tail = one_line((done.stderr or word).strip()[-120:], "no output")
        return "unknown", f"systemctl exit {done.returncode}: {tail}"

    def _launchd_interpreter(self) -> str | None:
        plist = (
            self._options.home / "Library/LaunchAgents" / f"{self._options.label}.plist"
        )
        try:
            payload = plistlib.loads(plist.read_bytes())
            first = payload["ProgramArguments"][0]
        except (
            OSError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            plistlib.InvalidFileException,
        ):
            return None
        return first if isinstance(first, str) and first else None

    def _systemd_interpreter(self) -> str | None:
        unit = (
            self._options.home
            / ".config/systemd/user"
            / f"{self._options.label}.service"
        )
        try:
            lines = unit.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in lines:
            if line.startswith("ExecStart="):
                try:
                    words = shlex.split(line.removeprefix("ExecStart="))
                except ValueError:
                    return None
                return words[0].lstrip("-@:+!") if words else None
        return None

    def _display(self, path: str) -> str:
        """The path as an alarm row may carry it: the home directory collapsed to ``~``."""
        home = str(self._options.home)
        return (
            "~" + path[len(home) :]
            if path == home or path.startswith(home + os.sep)
            else path
        )

    @staticmethod
    def _executable(path: str) -> bool:
        return os.path.isfile(path) and os.access(path, os.X_OK)

    @staticmethod
    def _read_status(path: Path) -> tuple[datetime, datetime | None] | None:
        """The drainer's last cycle and last confirmed publish; None when it says nothing usable."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        cycle = _epoch(raw.get("last_cycle_at"))
        if cycle is None:
            return None
        return cycle, _epoch(raw.get("last_publish_at"))

    @staticmethod
    def _journal_depth(journal: Path) -> int:
        try:
            return sum(1 for entry in journal.iterdir() if entry.suffix == ".json")
        except OSError:
            return 0
