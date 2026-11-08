# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's hourly schedule: render, install, uninstall and run it on a declared host.

The cadence is the contract's ``schedule`` block; the hosts, their venv and their unit environment
are the overlay's. The scheduler is a systemd user timer. ``uninstall`` is the rollback: it stops
the schedule and removes its unit files, and it changes nothing else. A unit that names a worktree
is refused before anything is written, because the host reconcile removes a tree a unit runs from.
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
from omnimarket.nodes.node_host_reading_effect.handlers.schedule import (
    ModelHostScheduleResult,
    run_command,
)
from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    NODE_NAME,
    ModelExposureHost,
    ModelExposureReadingOverlay,
)

NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ModelExposureCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    on_calendar: str
    randomized_delay: str
    persistent: bool
    description: str


class ModelExposureSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    state_directory: str
    timeout_seconds: int = Field(ge=1)
    cadence: ModelExposureCadence


def load_schedule(path: Path = CONTRACT_PATH) -> ModelExposureSchedule:
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelExposureSchedule.model_validate(contract["schedule"])


def _safe(path: Path | str, what: str) -> str:
    text = str(path)
    if not _SAFE_PATH.match(text):
        raise ValueError(
            f"{what} {text!r} holds a character a unit line cannot carry unquoted"
        )
    return text


def _assignment(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{name}={escaped}"\n'


def _substitute(text: str, root: str, user_home: str) -> str:
    return text.replace("$OMNI_HOME", root).replace("$HOME", user_home)


def render_systemd(
    schedule: ModelExposureSchedule,
    host: ModelExposureHost,
    *,
    omni_root: Path,
    home: Path,
    overlay_root: Path,
) -> tuple[str, str, str]:
    """The systemd user service text, the timer text and the venv interpreter they run."""
    user_home = _safe(home, "home")
    root = _safe(omni_root, "registry root")
    venv = Path(_substitute(host.venv, root, user_home))
    python = _safe(
        (venv if venv.is_absolute() else omni_root / venv) / "bin" / "python",
        "venv interpreter",
    )
    state = _safe(omni_root / schedule.state_directory, "state directory")
    environment = {
        "HOME": user_home,
        "OMNI_HOME": root,
        OVERLAY_ROOTS_ENV: _safe(overlay_root, "overlay root"),
        "PATH": f"{user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        **{
            name: _substitute(value, root, user_home)
            for name, value in host.environment.items()
        },
    }
    env_file = _safe(
        _substitute(host.environment_file, root, user_home), "environment file"
    )
    cadence = schedule.cadence
    service = (
        "# Rendered by the runner group exposure node (schedule install); the node contract owns\n"
        "# the cadence, so change the contract and install again rather than editing this file.\n"
        "# Rollback: schedule uninstall.\n"
        "[Unit]\nDescription=ONEX hourly runner group exposure reading (read-only)\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={schedule.timeout_seconds}\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"EnvironmentFile=-{env_file}\n"
        f"WorkingDirectory={root}\n"
        f"ExecStart={python} -m {NODE_MODULE} hourly\n"
        f"StandardOutput=append:{state}/exposure.out.log\n"
        f"StandardError=append:{state}/exposure.err.log\n"
        f"SyslogIdentifier={schedule.unit_name}\n"
    )
    timer = (
        f"# {cadence.description}\n"
        "[Unit]\nDescription=ONEX hourly runner group exposure reading tick\n"
        f"\n[Timer]\nOnCalendar={cadence.on_calendar}\n"
        f"RandomizedDelaySec={cadence.randomized_delay}\n"
        f"Persistent={'true' if cadence.persistent else 'false'}\n"
        f"Unit={schedule.unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer, python


class ExposureScheduler:
    """Install, remove and run the hourly schedule on one declared host."""

    def __init__(
        self,
        *,
        schedule: ModelExposureSchedule,
        overlay: ModelExposureReadingOverlay,
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

    def check(self) -> ModelHostScheduleResult:
        if not self.overlay.hosts:
            return ModelHostScheduleResult(
                exit_code=1, stderr="GAP no host is declared\n"
            )
        names = ", ".join(host.name for host in self.overlay.hosts)
        return ModelHostScheduleResult(exit_code=0, stdout=f"OK hosts: {names}\n")

    def _declared(self, name: str) -> ModelExposureHost | ModelHostScheduleResult:
        host = self.overlay.host(name)
        if host is not None:
            return host
        return ModelHostScheduleResult(
            exit_code=1,
            stderr=f"{name}: not in the {NODE_NAME} overlay's hosts, so the schedule is not declared for it\n",
        )

    def install(
        self, name: str, *, omni_root: Path, overlay_root: Path, dry_run: bool = False
    ) -> ModelHostScheduleResult:
        host = self._declared(name)
        if isinstance(host, ModelHostScheduleResult):
            return host
        try:
            service, timer, python = render_systemd(
                self.schedule,
                host,
                omni_root=omni_root,
                home=self.home,
                overlay_root=overlay_root,
            )
            # A unit that runs from a worktree dies when the host reconcile removes the tree:
            # refuse before anything is written, naming the path (OMN-22735).
            refuse_worktree_roots({self.service_name: service, self.timer_name: timer})
        except UnitRootRefusedError as exc:
            return ModelHostScheduleResult(
                exit_code=1,
                stderr=(
                    f"{exc}; declare a venv and commands under an installed unit's clone "
                    f"in the {NODE_NAME} overlay and install again\n"
                ),
            )
        except ValueError as exc:
            return ModelHostScheduleResult(exit_code=1, stderr=f"{exc}\n")
        files = [
            (self.unit_dir / self.service_name, service.encode("utf-8")),
            (self.unit_dir / self.timer_name, timer.encode("utf-8")),
        ]
        if dry_run:
            text = "".join(body.decode("utf-8") + "\n" for _, body in files)
            return ModelHostScheduleResult(exit_code=0, stdout=text)
        if not Path(python).is_file():
            return ModelHostScheduleResult(
                exit_code=1, stderr=f"no venv interpreter at {python}\n"
            )
        started = self.run([python, "-c", f"import {NODE_MODULE}.__main__"])
        if started.returncode:
            return ModelHostScheduleResult(
                exit_code=1,
                stderr=(
                    f"{python} cannot import {NODE_MODULE}.__main__ (exit {started.returncode}); "
                    f"the hourly run would not start\n{started.stderr}"
                ),
            )
        (omni_root / self.schedule.state_directory).mkdir(parents=True, exist_ok=True)
        output = ""
        for target, body in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.read_bytes() == body:
                output += f"{target.name} unchanged\n"
            else:
                target.write_bytes(body)
                output += f"wrote {target}\n"
        for step in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", self.timer_name],
        ):
            done = self.run(step)
            output += done.stdout
            if done.returncode:
                return ModelHostScheduleResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        return ModelHostScheduleResult(
            exit_code=0,
            stdout=output
            + f"installed on {host.name}: {self.schedule.cadence.description}\n",
        )

    def uninstall(self, name: str) -> ModelHostScheduleResult:
        """The rollback: stop the schedule and remove its unit files."""
        host = self._declared(name)
        if isinstance(host, ModelHostScheduleResult):
            return host
        output = ""
        done = self.run(["systemctl", "--user", "disable", "--now", self.timer_name])
        output += done.stdout
        for unit in (self.timer_name, self.service_name):
            target = self.unit_dir / unit
            if target.exists():
                target.unlink()
                output += f"removed {target}\n"
        reloaded = self.run(["systemctl", "--user", "daemon-reload"])
        output += reloaded.stdout
        return ModelHostScheduleResult(
            exit_code=reloaded.returncode, stdout=output, stderr=reloaded.stderr
        )

    def run_now(self, name: str) -> ModelHostScheduleResult:
        host = self._declared(name)
        if isinstance(host, ModelHostScheduleResult):
            return host
        done = self.run(["systemctl", "--user", "start", self.service_name])
        return ModelHostScheduleResult(
            exit_code=done.returncode,
            stdout=done.stdout or f"ran the schedule on {name} once\n",
            stderr=done.stderr,
        )

    def status(self, name: str) -> ModelHostScheduleResult:
        host = self._declared(name)
        if isinstance(host, ModelHostScheduleResult):
            return host
        done = self.run(["systemctl", "--user", "is-enabled", self.timer_name])
        return ModelHostScheduleResult(
            exit_code=0 if done.returncode == 0 else 1,
            stdout=done.stdout or done.stderr,
        )
