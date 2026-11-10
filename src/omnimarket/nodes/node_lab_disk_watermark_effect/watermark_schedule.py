# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render, install and report the watermark node's contract-owned systemd user timer."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelLabDiskWatermarkSchedule,
    ModelLabDiskWatermarkScheduleResult,
)

NODE_NAME = "node_lab_disk_watermark_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
OVERLAY_ENV = "ONEX_LAB_DISK_WATERMARK_OVERLAY"
UNIT_NAME = "onex-disk-watermark"
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
    schedule: ModelLabDiskWatermarkSchedule,
    host: str,
    *,
    python: Path,
    source_root: Path,
    overlay_file: Path,
    home: Path,
) -> tuple[str, str]:
    """The systemd user service and timer text of the watermark reading on one host."""
    interpreter = _safe(python, "interpreter")
    user_home = _safe(home, "home")
    environment = {
        "HOME": user_home,
        "PATH": f"{user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": _safe(source_root / "src", "source root"),
        OVERLAY_ENV: _safe(overlay_file, "overlay file"),
    }
    service = (
        "# Rendered by the lab disk watermark node (schedule install).\n"
        "[Unit]\nDescription=ONEX lab disk watermark (reading file and page past the page mark)\n"
        "\n[Service]\nType=oneshot\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"TimeoutStartSec={schedule.timeout_start_seconds}\n"
        f"ExecStart={_quote(interpreter)} -m {NODE_MODULE} run --host {_quote(host)}\n"
        f"SyslogIdentifier={UNIT_NAME}\n"
    )
    timer = (
        f"# {schedule.description}\n"
        "[Unit]\nDescription=ONEX lab disk watermark tick\n"
        f"\n[Timer]\nOnActiveSec={schedule.first_pass}\n"
        f"OnUnitActiveSec={schedule.interval_seconds}s\n"
        f"AccuracySec={schedule.accuracy}\nRandomizedDelaySec=0\n"
        f"Unit={UNIT_NAME}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


class WatermarkScheduler:
    """Install, check and report the watermark timer on one declared host."""

    def __init__(
        self,
        *,
        schedule: ModelLabDiskWatermarkSchedule,
        home: Path,
        run: Runner = run_command,
    ) -> None:
        self.schedule = schedule
        self.home = home
        self.run = run
        self.service_name = f"{UNIT_NAME}.service"
        self.timer_name = f"{UNIT_NAME}.timer"
        self.unit_dir = home / ".config/systemd/user"

    def install(
        self,
        host: str,
        *,
        python: Path,
        source_root: Path,
        overlay_file: Path,
        dry_run: bool = False,
    ) -> ModelLabDiskWatermarkScheduleResult:
        try:
            service, timer = render_units(
                self.schedule,
                host,
                python=python,
                source_root=source_root,
                overlay_file=overlay_file,
                home=self.home,
            )
        except ValueError as exc:
            return ModelLabDiskWatermarkScheduleResult(exit_code=1, stderr=f"{exc}\n")
        if dry_run:
            return ModelLabDiskWatermarkScheduleResult(
                exit_code=0, stdout=f"{service}\n{timer}"
            )
        if not python.is_file() or not overlay_file.is_file():
            return ModelLabDiskWatermarkScheduleResult(
                exit_code=1,
                stderr=f"no interpreter at {python} or no overlay at {overlay_file}\n",
            )
        probe = self.run(
            [
                "env",
                f"PYTHONPATH={source_root / 'src'}",
                str(python),
                "-c",
                f"import {NODE_MODULE}.__main__",
            ]
        )
        if probe.returncode:
            return ModelLabDiskWatermarkScheduleResult(
                exit_code=1,
                stderr=f"{python} cannot import {NODE_MODULE}.__main__ (exit {probe.returncode})\n{probe.stderr}",
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
                return ModelLabDiskWatermarkScheduleResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        listed = self.run(
            ["systemctl", "--user", "list-timers", self.timer_name, "--no-pager"]
        )
        output += listed.stdout
        if listed.returncode or self.timer_name not in listed.stdout:
            return ModelLabDiskWatermarkScheduleResult(
                exit_code=1,
                stdout=output,
                stderr=f"{self.timer_name} is enabled but systemd lists no schedule for it\n",
            )
        output += f"installed {self.timer_name}: {self.schedule.description}\n"
        return ModelLabDiskWatermarkScheduleResult(exit_code=0, stdout=output)

    def status(self) -> ModelLabDiskWatermarkScheduleResult:
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
        return ModelLabDiskWatermarkScheduleResult(
            exit_code=0 if loaded else 1, stdout=output
        )

    def run_now(self) -> ModelLabDiskWatermarkScheduleResult:
        started = self.run(["systemctl", "--user", "start", self.service_name])
        return ModelLabDiskWatermarkScheduleResult(
            exit_code=started.returncode,
            stdout=started.stdout or f"ran {self.service_name} once\n",
            stderr=started.stderr,
        )

    def uninstall(self) -> ModelLabDiskWatermarkScheduleResult:
        disabled = self.run(
            ["systemctl", "--user", "disable", "--now", self.timer_name]
        )
        (self.unit_dir / self.service_name).unlink(missing_ok=True)
        (self.unit_dir / self.timer_name).unlink(missing_ok=True)
        reloaded = self.run(["systemctl", "--user", "daemon-reload"])
        return ModelLabDiskWatermarkScheduleResult(
            exit_code=disabled.returncode or reloaded.returncode,
            stdout=f"removed {self.timer_name} and {self.service_name}\n",
            stderr=disabled.stderr + reloaded.stderr,
        )
