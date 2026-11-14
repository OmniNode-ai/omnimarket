# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local backends for the lab-fill launcher effect.

``LocalFireFiles`` reads the fire's result file and run log, checks the Linear
snapshot, and writes the latest file atomically. ``LocalAlertSender`` resolves
the alert-channel library from the overlay and runs it via the shell.
``SystemClock`` returns the current UTC date.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path

from omnimarket.nodes.node_lab_fill_launcher_effect.overlay import (
    ModelLabFillLauncherAlert,
    ModelLabFillLauncherState,
)


class LocalFireFiles:
    """File I/O for the fire's result, run log, snapshot and latest file.

    The fire's own files live under ``<home>/<state_relpath>``; the Linear snapshot of the day lives under
    ``<home>/<snapshot_relpath>``. Both relative paths are overlay values.
    """

    def __init__(self, home: Path, state: ModelLabFillLauncherState) -> None:
        self._home = home
        self._state = state
        self.state_dir = home / state.state_relpath

    def read_result(self, fire_id: str) -> tuple[bool, bool, str | None]:
        """(exists, nonempty, text) for <state_dir>/<results_dirname>/<fire_id>.json."""
        path = self.state_dir / self._state.results_dirname / f"{fire_id}.json"
        if not path.exists():
            return (False, False, None)
        nonempty = path.stat().st_size > 0
        text = path.read_text(encoding="utf-8") if nonempty else None
        return (True, nonempty, text)

    def read_log(self, path: str) -> str:
        """The run log text ("" when unreadable)."""
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def snapshot_present(self, name: str) -> bool:
        """Whether the Linear snapshot file exists under the snapshot relpath."""
        return (self._home / self._state.snapshot_relpath / name).is_file()

    def latest_path(self) -> str:
        """Where the validated result is published."""
        return str(self.state_dir / self._state.latest_filename)

    def write_latest(self, value: Mapping[str, object]) -> None:
        """Atomically publish the validated result (temp file beside it, then replace)."""
        target = Path(self.latest_path())
        temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
        temporary.replace(target)


class LocalAlertSender:
    """Delivers alerts via the alert-channel shell library."""

    def __init__(
        self,
        alert: ModelLabFillLauncherAlert,
        state: ModelLabFillLauncherState,
        environ: Mapping[str, str],
    ) -> None:
        self._alert = alert
        self._home_env = state.home_env
        self._environ = environ

    def send(self, category: str, text: str) -> int:
        """Run the alert-channel library; return its exit code."""
        lib = self._environ.get(self._alert.lib_env) or str(
            Path(self._environ.get(self._home_env, "")) / self._alert.lib_relpath
        )
        if not os.access(lib, os.R_OK):
            return self._alert.unavailable_exit
        script = 'set -u; . "$1" && "$2" "$3" "$4"'
        try:
            proc = subprocess.run(
                [
                    self._alert.shell,
                    "-c",
                    script,
                    "_",
                    lib,
                    self._alert.function,
                    category,
                    text,
                ],
                capture_output=True,
                timeout=self._alert.timeout_seconds,
            )
            return proc.returncode
        except (subprocess.TimeoutExpired, OSError):
            return self._alert.unavailable_exit


class SystemClock:
    """Returns the current UTC date."""

    def today_utc(self) -> date:
        return datetime.now(UTC).date()
