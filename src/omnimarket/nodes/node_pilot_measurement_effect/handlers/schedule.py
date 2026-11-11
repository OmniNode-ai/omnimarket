# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's hourly schedule: render, install, uninstall and run it on a declared lab host.

The cadence is the contract's ``schedule`` block; the hosts and their unit environment are the
overlay's ``schedule_hosts``. A lab host gets a systemd user timer. ``uninstall`` is the rollback:
it stops the schedule and removes its unit files, and it changes nothing else.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.rules_unit_roots import (
    UnitRootRefusedError,
    refuse_worktree_roots,
)
from omnimarket.nodes.node_pilot_measurement_effect.overlay import (
    NODE_NAME,
    ModelPilotMeasurementOverlay,
    ModelPilotScheduleHost,
)

NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ModelPilotCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    on_calendar: str
    randomized_delay: str
    persistent: bool
    description: str


class ModelPilotSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    state_directory: str
    timeout_seconds: int = Field(ge=1)
    cadence: ModelPilotCadence


class ModelScheduleResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    stdout: str = ""
    stderr: str = ""


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command), capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(command), 127, "", str(exc))


def load_schedule(path: Path = CONTRACT_PATH) -> ModelPilotSchedule:
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelPilotSchedule.model_validate(contract["schedule"])


def _safe(path: Path, what: str) -> str:
    text = str(path)
    if not _SAFE_PATH.match(text):
        raise ValueError(
            f"{what} {text!r} holds a character a unit line cannot carry unquoted"
        )
    return text


def _assignment(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{name}={escaped}"\n'


def _unit_facts(
    host: ModelPilotScheduleHost,
    *,
    omni_root: Path,
    home: Path,
    overlay_root: Path,
    schedule: ModelPilotSchedule,
) -> tuple[str, str, str, dict[str, str]]:
    """Registry root, interpreter, state directory and unit environment, shared by every unit."""
    root = _safe(omni_root, "registry root")
    user_home = _safe(home, "home")
    python = _safe(omni_root / host.venv / "bin" / "python", "venv interpreter")
    state = _safe(omni_root / schedule.state_directory, "state directory")
    environment = {
        "HOME": user_home,
        "OMNI_HOME": root,
        OVERLAY_ROOTS_ENV: _safe(overlay_root, "overlay root"),
        "PATH": f"{user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        **{
            name: value.replace("$OMNI_HOME", root).replace("$HOME", user_home)
            for name, value in host.environment.items()
        },
    }
    return root, python, state, environment


def render_systemd(
    schedule: ModelPilotSchedule,
    host: ModelPilotScheduleHost,
    *,
    omni_root: Path,
    home: Path,
    overlay_root: Path,
) -> tuple[str, str]:
    """The systemd user service and timer text."""
    root, python, state, environment = _unit_facts(
        host,
        omni_root=omni_root,
        home=home,
        overlay_root=overlay_root,
        schedule=schedule,
    )
    cadence = schedule.cadence
    service = (
        "# Rendered by the pilot measurement node (schedule install); the node contract owns the\n"
        "# cadence, so change the contract and install again rather than editing this file.\n"
        "# Rollback: schedule uninstall.\n"
        "[Unit]\nDescription=ONEX hourly pilot measurement reading (piece 16)\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={schedule.timeout_seconds}\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={root}\n"
        f"ExecStart={python} -m {NODE_MODULE} daily\n"
        f"StandardOutput=append:{state}/pilot-measurement.out.log\n"
        f"StandardError=append:{state}/pilot-measurement.err.log\n"
        f"SyslogIdentifier={schedule.unit_name}\n"
    )
    timer = (
        f"# {cadence.description}\n"
        "[Unit]\nDescription=ONEX hourly pilot measurement reading tick\n"
        f"\n[Timer]\nOnCalendar={cadence.on_calendar}\n"
        f"RandomizedDelaySec={cadence.randomized_delay}\n"
        f"Persistent={'true' if cadence.persistent else 'false'}\n"
        f"Unit={schedule.unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


class PilotMeasurementScheduler:
    """Install, remove, read and run the hourly schedule on one declared host."""

    def __init__(
        self,
        *,
        schedule: ModelPilotSchedule,
        overlay: ModelPilotMeasurementOverlay,
        home: Path,
        run: Runner = run_command,
    ) -> None:
        self.schedule = schedule
        self.overlay = overlay
        self.home = home
        self.run = run
        self.service_name = f"{schedule.unit_name}.service"
        self.timer_name = f"{schedule.unit_name}.timer"
        self.unit_dir = home / ".config/systemd/user"

    def _declared(self, name: str) -> ModelPilotScheduleHost | ModelScheduleResult:
        host = self.overlay.host(name)
        if host is not None:
            return host
        return ModelScheduleResult(
            exit_code=1,
            stderr=f"{name}: not in the {NODE_NAME} overlay's schedule_hosts\n",
        )

    def install(
        self, name: str, *, omni_root: Path, overlay_root: Path, dry_run: bool = False
    ) -> ModelScheduleResult:
        host = self._declared(name)
        if isinstance(host, ModelScheduleResult):
            return host
        try:
            service, timer = render_systemd(
                self.schedule,
                host,
                omni_root=omni_root,
                home=self.home,
                overlay_root=overlay_root,
            )
        except ValueError as exc:
            return ModelScheduleResult(exit_code=1, stderr=f"{exc}\n")
        # A unit that runs from a worktree dies when the host reconcile removes the tree: refuse
        # before anything is written or printed, naming the path (OMN-22783).
        try:
            refuse_worktree_roots(
                {
                    self.service_name: service,
                    self.timer_name: timer,
                }
            )
        except UnitRootRefusedError as exc:
            return ModelScheduleResult(
                exit_code=1,
                stderr=(
                    f"{exc}; declare a venv and commands under the registry root's clone "
                    f"in the {NODE_NAME} overlay and install again\n"
                ),
            )
        files = [
            (self.unit_dir / self.service_name, service),
            (self.unit_dir / self.timer_name, timer),
        ]
        if dry_run:
            return ModelScheduleResult(
                exit_code=0, stdout="".join(body + "\n" for _, body in files)
            )
        python = omni_root / host.venv / "bin" / "python"
        started = self.run([str(python), "-c", f"import {NODE_MODULE}.__main__"])
        if started.returncode:
            return ModelScheduleResult(
                exit_code=1,
                stderr=f"{python} cannot import {NODE_MODULE}.__main__ (exit "
                f"{started.returncode}); the hourly run would not start\n",
            )
        (omni_root / self.schedule.state_directory).mkdir(parents=True, exist_ok=True)
        output = ""
        for target, body in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            output += f"wrote {target}\n"
        for step in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", self.timer_name],
        ):
            done = self.run(step)
            if done.returncode:
                return ModelScheduleResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        return ModelScheduleResult(
            exit_code=0,
            stdout=output
            + f"installed on {host.name}: {self.schedule.cadence.description}\n",
        )

    def uninstall(self, name: str) -> ModelScheduleResult:
        """The rollback: stop the schedule and remove its unit files."""
        host = self._declared(name)
        if isinstance(host, ModelScheduleResult):
            return host
        self.run(["systemctl", "--user", "disable", "--now", self.timer_name])
        output = ""
        for target in (
            self.unit_dir / self.timer_name,
            self.unit_dir / self.service_name,
        ):
            if target.exists():
                target.unlink()
                output += f"removed {target}\n"
        self.run(["systemctl", "--user", "daemon-reload"])
        return ModelScheduleResult(
            exit_code=0, stdout=output + f"uninstalled on {name}\n"
        )

    def status(self, name: str) -> ModelScheduleResult:
        host = self._declared(name)
        if isinstance(host, ModelScheduleResult):
            return host
        done = self.run(
            [
                "systemctl",
                "--user",
                "list-timers",
                self.timer_name,
                "--all",
            ]
        )
        return ModelScheduleResult(
            exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr
        )

    def run_now(self, name: str) -> ModelScheduleResult:
        host = self._declared(name)
        if isinstance(host, ModelScheduleResult):
            return host
        done = self.run(["systemctl", "--user", "start", self.service_name])
        return ModelScheduleResult(
            exit_code=done.returncode, stdout=done.stdout, stderr=done.stderr
        )


__all__ = [
    "ModelPilotCadence",
    "ModelPilotSchedule",
    "ModelScheduleResult",
    "PilotMeasurementScheduler",
    "load_schedule",
    "render_systemd",
]
