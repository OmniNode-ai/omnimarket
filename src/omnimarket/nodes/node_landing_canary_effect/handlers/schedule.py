# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's daily schedule: render, install, uninstall and run it on a declared host, from the registry root.

The cadence is the contract's ``schedule`` block; the hosts, their scheduler and their unit
environment are the overlay's. The unit runs from the registry root and from nowhere else: a root
under ``omni_worktrees`` is refused, because a worktree is removed the tick after its work merges and
the daily run would then fail silently. A Linux host gets a systemd user timer and the operator's Mac
a launchd agent. ``uninstall`` is the rollback: it stops the schedule and removes its unit files, and
it changes nothing else.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.rules_unit_roots import (
    refuse_worktree_plists,
    refuse_worktree_roots,
)
from omnimarket.nodes.node_landing_canary_effect.overlay import (
    NODE_NAME,
    ModelCanaryHost,
    ModelLandingCanaryOverlay,
)

NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ModelLandingCanaryCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    on_calendar: str
    launchd_minute: int = Field(ge=0, le=59)
    launchd_hour: int = Field(ge=0, le=23)
    randomized_delay: str
    persistent: bool
    description: str


class ModelLandingCanarySchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    launchd_label: str = Field(min_length=1)
    state_directory: str
    timeout_seconds: int = Field(ge=1)
    cadence: ModelLandingCanaryCadence


class ModelCanaryScheduleResult(BaseModel):
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


def load_schedule(path: Path = CONTRACT_PATH) -> ModelLandingCanarySchedule:
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelLandingCanarySchedule.model_validate(contract["schedule"])


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


def _unit_environment(
    host: ModelCanaryHost, omni_root: Path, home: Path, overlay_root: Path
) -> dict[str, str]:
    user_home = _safe(home, "home")
    root = _safe(omni_root, "registry root")
    return {
        "HOME": user_home,
        "OMNI_HOME": root,
        OVERLAY_ROOTS_ENV: _safe(overlay_root, "overlay root"),
        "PATH": f"{user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        # The overlay names no home directory: its values start from these two, filled here.
        **{
            name: value.replace("$OMNI_HOME", root).replace("$HOME", user_home)
            for name, value in host.environment.items()
        },
    }


def render_systemd(
    schedule: ModelLandingCanarySchedule,
    host: ModelCanaryHost,
    *,
    omni_root: Path,
    home: Path,
    overlay_root: Path,
) -> tuple[str, str]:
    """The systemd user service and timer text."""
    root = _safe(omni_root, "registry root")
    python = _safe(omni_root / host.venv / "bin" / "python", "venv interpreter")
    state = _safe(omni_root / schedule.state_directory, "state directory")
    environment = _unit_environment(host, omni_root, home, overlay_root)
    cadence = schedule.cadence
    service = (
        "# Rendered by the landing canary node (schedule install); the node contract owns the\n"
        "# cadence, so change the contract and install again rather than editing this file.\n"
        "# Rollback: schedule uninstall.\n"
        "[Unit]\nDescription=ONEX daily landing canary (one test pull request)\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={schedule.timeout_seconds}\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={root}\n"
        f"ExecStart={python} -m {NODE_MODULE} daily\n"
        f"StandardOutput=append:{state}/landing-canary.out.log\n"
        f"StandardError=append:{state}/landing-canary.err.log\n"
        f"SyslogIdentifier={schedule.unit_name}\n"
    )
    timer = (
        f"# {cadence.description}\n"
        "[Unit]\nDescription=ONEX daily landing canary tick\n"
        f"\n[Timer]\nOnCalendar={cadence.on_calendar}\n"
        f"RandomizedDelaySec={cadence.randomized_delay}\n"
        f"Persistent={'true' if cadence.persistent else 'false'}\n"
        f"Unit={schedule.unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def render_launchd(
    schedule: ModelLandingCanarySchedule,
    host: ModelCanaryHost,
    *,
    omni_root: Path,
    home: Path,
    overlay_root: Path,
) -> bytes:
    """The launchd agent, as plist bytes."""
    python = _safe(omni_root / host.venv / "bin" / "python", "venv interpreter")
    state = _safe(omni_root / schedule.state_directory, "state directory")
    return plistlib.dumps(
        {
            "Label": schedule.launchd_label,
            "ProgramArguments": [python, "-m", NODE_MODULE, "daily"],
            "EnvironmentVariables": _unit_environment(
                host, omni_root, home, overlay_root
            ),
            "WorkingDirectory": _safe(omni_root, "registry root"),
            "StartCalendarInterval": {
                "Hour": schedule.cadence.launchd_hour,
                "Minute": schedule.cadence.launchd_minute,
            },
            "StandardOutPath": f"{state}/landing-canary.out.log",
            "StandardErrorPath": f"{state}/landing-canary.err.log",
        }
    )


class LandingCanaryScheduler:
    """Install, remove and run the daily schedule on one declared host."""

    def __init__(
        self,
        *,
        schedule: ModelLandingCanarySchedule,
        overlay: ModelLandingCanaryOverlay,
        home: Path,
        run: Runner = run_command,
        uid: int | None = None,
    ) -> None:
        self.schedule = schedule
        self.overlay = overlay
        self.home = home
        self.run = run
        self.uid = os.getuid() if uid is None else uid
        self.service_name = f"{schedule.unit_name}.service"
        self.timer_name = f"{schedule.unit_name}.timer"
        self.unit_dir = home / ".config/systemd/user"
        self.plist = home / "Library/LaunchAgents" / f"{schedule.launchd_label}.plist"
        self.target = f"gui/{self.uid}/{schedule.launchd_label}"

    def check(self) -> ModelCanaryScheduleResult:
        if not self.overlay.hosts:
            return ModelCanaryScheduleResult(
                exit_code=1, stderr="GAP no host is declared\n"
            )
        names = ", ".join(host.name for host in self.overlay.hosts)
        return ModelCanaryScheduleResult(exit_code=0, stdout=f"OK hosts: {names}\n")

    def _declared(self, name: str) -> ModelCanaryHost | ModelCanaryScheduleResult:
        host = self.overlay.host(name)
        if host is not None:
            return host
        return ModelCanaryScheduleResult(
            exit_code=1,
            stderr=f"{name}: not in the {NODE_NAME} overlay's hosts, so the schedule is not declared for it\n",
        )

    def install(
        self, name: str, *, omni_root: Path, overlay_root: Path, dry_run: bool = False
    ) -> ModelCanaryScheduleResult:
        host = self._declared(name)
        if isinstance(host, ModelCanaryScheduleResult):
            return host
        if not omni_root.is_absolute():
            return ModelCanaryScheduleResult(
                exit_code=1,
                stderr=f"registry root {omni_root} is not an absolute path; the schedule installs from the registry root\n",
            )
        render = {
            "omni_root": omni_root,
            "home": self.home,
            "overlay_root": overlay_root,
        }
        try:
            if host.scheduler == "launchd":
                rendered = render_launchd(self.schedule, host, **render)
                files = [(self.plist, rendered)]
            else:
                service, timer = render_systemd(self.schedule, host, **render)
                files = [
                    (self.unit_dir / self.service_name, service.encode("utf-8")),
                    (self.unit_dir / self.timer_name, timer.encode("utf-8")),
                ]
            if host.scheduler == "launchd":
                refuse_worktree_plists({target.name: body for target, body in files})
            else:
                refuse_worktree_roots(
                    {target.name: body.decode("utf-8") for target, body in files}
                )
        except ValueError as exc:
            return ModelCanaryScheduleResult(exit_code=1, stderr=f"{exc}\n")
        no_room = self._no_room_after_the_bar()
        if no_room:
            return ModelCanaryScheduleResult(exit_code=1, stderr=f"{no_room}\n")
        if dry_run:
            text = "".join(body.decode("utf-8") + "\n" for _, body in files)
            return ModelCanaryScheduleResult(exit_code=0, stdout=text)
        python = omni_root / host.venv / "bin" / "python"
        if not python.is_file():
            return ModelCanaryScheduleResult(
                exit_code=1, stderr=f"no venv interpreter at {python}\n"
            )
        started = self.run([str(python), "-c", f"import {NODE_MODULE}.__main__"])
        if started.returncode:
            return ModelCanaryScheduleResult(
                exit_code=1,
                stderr=(
                    f"{python} cannot import {NODE_MODULE}.__main__ (exit {started.returncode}); "
                    f"the daily run would not start\n{started.stderr}"
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
        return self._activate(host, output)

    def _no_room_after_the_bar(self) -> str:
        """Empty when the scheduled run's timeout leaves room to publish after the landing bar runs out.

        After the bar the run still reads the pull request once more and appends its row, each bounded by
        the overlay's command timeouts; a timeout without that room kills the run before it publishes.
        """
        target = self.overlay.target
        room = self.overlay.ledger_append.timeout_seconds
        if target.scm is not None:
            room += target.scm.command_timeout_seconds
        needed = target.landing_bound_seconds + room
        if self.schedule.timeout_seconds >= needed:
            return ""
        return (
            f"the schedule timeout {self.schedule.timeout_seconds}s leaves no room to publish after the "
            f"landing bound {target.landing_bound_seconds}s (needs at least {needed}s)"
        )

    def _activate(
        self, host: ModelCanaryHost, output: str
    ) -> ModelCanaryScheduleResult:
        if host.scheduler == "launchd":
            # A loaded agent keeps its old definition until it is booted out.
            self.run(["launchctl", "bootout", self.target])
            steps = [
                ["launchctl", "bootstrap", f"gui/{self.uid}", str(self.plist)],
            ]
        else:
            steps = [
                ["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", self.timer_name],
            ]
        for step in steps:
            done = self.run(step)
            output += done.stdout
            if done.returncode:
                return ModelCanaryScheduleResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        return ModelCanaryScheduleResult(
            exit_code=0,
            stdout=output
            + f"installed on {host.name}: {self.schedule.cadence.description}\n",
        )

    def uninstall(self, name: str) -> ModelCanaryScheduleResult:
        """The rollback: stop the schedule and remove its unit files."""
        host = self._declared(name)
        if isinstance(host, ModelCanaryScheduleResult):
            return host
        output = ""
        if host.scheduler == "launchd":
            done = self.run(["launchctl", "bootout", self.target])
            output += done.stdout
            self.plist.unlink(missing_ok=True)
            return ModelCanaryScheduleResult(
                exit_code=0, stdout=output + f"removed {self.plist}\n"
            )
        # Stopping the timer ends future triggers only: a service it already started keeps running, so
        # it is stopped too. A failure to stop either is a failed rollback and the unit files stay, so
        # the rollback can be run again. A unit that was never installed has nothing to stop.
        installed = any(
            (self.unit_dir / unit).exists()
            for unit in (self.timer_name, self.service_name)
        )
        failures: list[subprocess.CompletedProcess[str]] = []
        for step in (
            ["systemctl", "--user", "disable", "--now", self.timer_name],
            ["systemctl", "--user", "stop", self.service_name],
        ):
            done = self.run(step)
            output += done.stdout
            if done.returncode and installed:
                failures.append(done)
        if failures:
            return ModelCanaryScheduleResult(
                exit_code=failures[0].returncode,
                stdout=output,
                stderr="rollback incomplete, unit files kept: "
                + " ".join(failure.stderr.strip() for failure in failures)
                + "\n",
            )
        for unit in (self.timer_name, self.service_name):
            target = self.unit_dir / unit
            if target.exists():
                target.unlink()
                output += f"removed {target}\n"
        reloaded = self.run(["systemctl", "--user", "daemon-reload"])
        output += reloaded.stdout
        return ModelCanaryScheduleResult(
            exit_code=reloaded.returncode, stdout=output, stderr=reloaded.stderr
        )

    def run_now(self, name: str) -> ModelCanaryScheduleResult:
        host = self._declared(name)
        if isinstance(host, ModelCanaryScheduleResult):
            return host
        command = (
            ["launchctl", "kickstart", self.target]
            if host.scheduler == "launchd"
            else ["systemctl", "--user", "start", self.service_name]
        )
        done = self.run(command)
        return ModelCanaryScheduleResult(
            exit_code=done.returncode,
            stdout=done.stdout or f"ran the schedule on {name} once\n",
            stderr=done.stderr,
        )

    def status(self, name: str) -> ModelCanaryScheduleResult:
        host = self._declared(name)
        if isinstance(host, ModelCanaryScheduleResult):
            return host
        command = (
            ["launchctl", "print", self.target]
            if host.scheduler == "launchd"
            else ["systemctl", "--user", "is-enabled", self.timer_name]
        )
        done = self.run(command)
        return ModelCanaryScheduleResult(
            exit_code=0 if done.returncode == 0 else 1,
            stdout=done.stdout or done.stderr,
        )
