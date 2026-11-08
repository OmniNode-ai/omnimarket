# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render, install and report the retention node's contract-owned systemd user timer."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.nodes.node_temp_retention_effect.models.model_temp_retention import (
    ModelRetentionSchedule,
    ModelRetentionScheduleResult,
    ModelTempRetentionHost,
)

NODE_NAME = "node_temp_retention_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
OVERLAY_ENV = "ONEX_TEMP_RETENTION_OVERLAY"
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a host command; a missing program reads as exit status 127."""
    try:
        return subprocess.run(
            list(command), capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(command), 127, "", str(exc))


def _safe(path: str | Path, what: str) -> str:
    text = str(path)
    if not Path(text).is_absolute():
        raise ValueError(f"{what} {text!r} must be an absolute path")
    if not _SAFE_PATH.match(text):
        raise ValueError(
            f"{what} {text!r} holds a character a unit line cannot carry unquoted"
        )
    return text


def _quote(value: str) -> str:
    """Quote one systemd command-line word, escaping specifiers and variables."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def _assignment(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{name}={escaped}"\n'


def render_units(
    schedule: ModelRetentionSchedule,
    row: ModelTempRetentionHost,
    *,
    python: Path,
    source_root: Path,
    overlay_file: Path,
    home: Path,
) -> tuple[str, str]:
    """The systemd user service and timer text of the retention pass on one host."""
    interpreter = _safe(python, "interpreter")
    packages = _safe(source_root / "src", "source root")
    overlay = _safe(overlay_file, "overlay file")
    user_home = _safe(home, "home")
    cadence = schedule.cadence
    environment = {
        "HOME": user_home,
        "PATH": f"{user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": packages,
        OVERLAY_ENV: overlay,
    }
    service = (
        "# Rendered by the temp retention node (schedule install).\n"
        "[Unit]\nDescription=ONEX temp-directory retention (unheld entries past the run windows, package store ceilings)\n"
        "\n[Service]\nType=oneshot\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"TimeoutStartSec={cadence.timeout_start_seconds}\n"
        f"ExecStart={_quote(interpreter)} -m {NODE_MODULE} run --host {_quote(row.name)}\n"
        f"SyslogIdentifier={schedule.unit_name}\n"
    )
    timer = (
        f"# {cadence.description}\n"
        "[Unit]\nDescription=ONEX temp-directory retention tick\n"
        f"\n[Timer]\nOnActiveSec={cadence.first_pass}\n"
        f"OnUnitActiveSec={cadence.interval_seconds}s\n"
        f"AccuracySec={cadence.accuracy}\nRandomizedDelaySec=0\n"
        f"Unit={schedule.unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


class RetentionScheduler:
    """Install, check and report the retention timer on one declared host."""

    def __init__(
        self,
        *,
        schedule: ModelRetentionSchedule,
        rows: Sequence[ModelTempRetentionHost],
        home: Path,
        run: Runner = run_command,
    ) -> None:
        self.schedule = schedule
        self.rows = tuple(rows)
        self.home = home
        self.run = run
        self.service_name = f"{schedule.unit_name}.service"
        self.timer_name = f"{schedule.unit_name}.timer"
        self.unit_dir = home / ".config/systemd/user"

    def _row(self, host: str) -> ModelTempRetentionHost | ModelRetentionScheduleResult:
        for row in self.rows:
            if row.name == host:
                return row
        return ModelRetentionScheduleResult(
            exit_code=1,
            stderr=f"{host}: not in the {NODE_NAME} overlay, so no retention is declared for it\n",
        )

    def install(
        self,
        host: str,
        *,
        python: Path,
        source_root: Path,
        overlay_file: Path,
        dry_run: bool = False,
    ) -> ModelRetentionScheduleResult:
        row = self._row(host)
        if isinstance(row, ModelRetentionScheduleResult):
            return row
        try:
            service, timer = render_units(
                self.schedule,
                row,
                python=python,
                source_root=source_root,
                overlay_file=overlay_file,
                home=self.home,
            )
        except ValueError as exc:
            return ModelRetentionScheduleResult(exit_code=1, stderr=f"{exc}\n")
        if dry_run:
            return ModelRetentionScheduleResult(
                exit_code=0, stdout=f"{service}\n{timer}"
            )
        if not python.is_file():
            return ModelRetentionScheduleResult(
                exit_code=1, stderr=f"no interpreter at {python}\n"
            )
        if not overlay_file.is_file():
            return ModelRetentionScheduleResult(
                exit_code=1, stderr=f"no overlay file at {overlay_file}\n"
            )
        started = self.run(
            [
                "env",
                f"PYTHONPATH={source_root / 'src'}",
                str(python),
                "-c",
                f"import {NODE_MODULE}.__main__",
            ]
        )
        if started.returncode:
            return ModelRetentionScheduleResult(
                exit_code=1,
                stderr=(
                    f"{python} cannot import {NODE_MODULE}.__main__ from {source_root / 'src'} "
                    f"(exit {started.returncode}); the pass would not start\n{started.stderr}"
                ),
            )
        output = ""
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        for name, text in ((self.service_name, service), (self.timer_name, timer)):
            target = self.unit_dir / name
            if target.exists() and target.read_text(encoding="utf-8") == text:
                output += f"{name} unchanged\n"
            else:
                target.write_text(text, encoding="utf-8")
                output += f"wrote {target}\n"
        for args in (["daemon-reload"], ["enable", "--now", self.timer_name]):
            done = self.run(["systemctl", "--user", *args])
            output += done.stdout
            if done.returncode:
                return ModelRetentionScheduleResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        listed = self.run(
            ["systemctl", "--user", "list-timers", self.timer_name, "--no-pager"]
        )
        output += listed.stdout
        if listed.returncode or self.timer_name not in listed.stdout:
            return ModelRetentionScheduleResult(
                exit_code=1,
                stdout=output,
                stderr=f"{self.timer_name} is enabled but systemd lists no schedule for it\n",
            )
        output += f"installed {self.timer_name}: {self.schedule.cadence.description}\n"
        return ModelRetentionScheduleResult(exit_code=0, stdout=output)

    def status(self, host: str) -> ModelRetentionScheduleResult:
        row = self._row(host)
        if isinstance(row, ModelRetentionScheduleResult):
            return row
        active = self.run(["systemctl", "--user", "is-active", self.timer_name])
        loaded = active.returncode == 0
        output = f"{'LOADED' if loaded else 'NOT LOADED'} {self.timer_name}\n"
        if loaded:
            output += self.run(
                ["systemctl", "--user", "list-timers", self.timer_name, "--no-pager"]
            ).stdout
            output += self.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    self.service_name,
                    "-p",
                    "Result",
                    "-p",
                    "ExecMainStatus",
                    "-p",
                    "ExecMainExitTimestamp",
                ]
            ).stdout
        return ModelRetentionScheduleResult(exit_code=0 if loaded else 1, stdout=output)

    def run_now(self, host: str) -> ModelRetentionScheduleResult:
        row = self._row(host)
        if isinstance(row, ModelRetentionScheduleResult):
            return row
        started = self.run(["systemctl", "--user", "start", self.service_name])
        if started.returncode:
            return ModelRetentionScheduleResult(
                exit_code=started.returncode,
                stdout=started.stdout,
                stderr=started.stderr,
            )
        return ModelRetentionScheduleResult(
            exit_code=0, stdout=f"ran {self.service_name} once\n"
        )

    def uninstall(self, host: str) -> ModelRetentionScheduleResult:
        row = self._row(host)
        if isinstance(row, ModelRetentionScheduleResult):
            return row
        disabled = self.run(
            ["systemctl", "--user", "disable", "--now", self.timer_name]
        )
        (self.unit_dir / self.service_name).unlink(missing_ok=True)
        (self.unit_dir / self.timer_name).unlink(missing_ok=True)
        reloaded = self.run(["systemctl", "--user", "daemon-reload"])
        return ModelRetentionScheduleResult(
            exit_code=disabled.returncode or reloaded.returncode,
            stdout=f"removed {self.timer_name} and {self.service_name}\n",
            stderr=disabled.stderr + reloaded.stderr,
        )
