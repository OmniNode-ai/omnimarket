# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev deploy reading's own schedule: render, check, install, uninstall and run it on a
host.

The cadence is the contract's ``schedule`` block and the hosts are the overlay's
``schedule_hosts``. The only scheduler is a systemd user timer, which survives a reboot when the
host's user lingers and the timer is ``Persistent``. ``install`` writes the two unit files and
enables the timer; ``uninstall`` is the rollback: it disables and stops the timer and removes the
two files, and it changes nothing else.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.rules_unit_roots import refuse_worktree_roots
from omnimarket.nodes.node_dev_deploy_reading_effect.overlay import (
    NODE_NAME,
    ModelDevDeployReadingOverlay,
)

NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ModelCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    on_calendar: str
    randomized_delay: str
    persistent: bool
    description: str


class ModelDrillSchedule(BaseModel):
    """The drill's own unit: the drill verb, which adds the overlay's injected unreceipted merge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cadence: ModelCadence


class ModelDevDeployReadingSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state_directory: str
    timeout_seconds: int = Field(ge=1)
    cadence: ModelCadence
    schedulers: dict[str, str]
    #: Installed only where the overlay names a drill overlay root.
    drill: ModelDrillSchedule | None = None


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


def load_schedule(path: Path = CONTRACT_PATH) -> ModelDevDeployReadingSchedule:
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelDevDeployReadingSchedule.model_validate(contract["schedule"])


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


def _quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def render_units(
    schedule: ModelDevDeployReadingSchedule,
    overlay: ModelDevDeployReadingOverlay,
    *,
    omni_root: Path,
    venv: Path,
    home: Path,
    overlay_root: Path,
    drill: bool = False,
) -> tuple[str, str]:
    """The systemd user service and timer text; ``drill`` renders the drill's unit pair."""
    if drill and schedule.drill is None:
        raise ValueError("the contract declares no drill schedule")
    unit_name = (
        overlay.schedule_units.drill_unit_name
        if drill
        else overlay.schedule_units.unit_name
    )
    cadence = schedule.drill.cadence if drill and schedule.drill else schedule.cadence
    title = (
        "ONEX daily dev deploy reading drill (piece 11; an unreceipted synthetic merge, the error event)"
        if drill
        else "ONEX dev deploy reading (piece 11; read-only reads, reading rows only)"
    )
    log_stem = "dev-deploy-reading-drill" if drill else "dev-deploy-reading"
    tick = (
        "ONEX daily dev deploy reading drill tick"
        if drill
        else "ONEX dev deploy reading tick"
    )
    root = _safe(omni_root, "registry root")
    python = _safe(venv / "bin" / "python", "venv interpreter")
    user_home = _safe(home, "home")
    state = _safe(omni_root / schedule.state_directory, "state directory")
    environment = {
        "HOME": user_home,
        "OMNI_HOME": root,
        OVERLAY_ROOTS_ENV: _safe(overlay_root, "overlay root"),
        "PATH": f"{user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        **overlay.unit_environment,
    }
    service = (
        "# Rendered by the dev deploy reading node (schedule install); the node contract owns\n"
        "# the cadence, so change the contract and install again rather than editing this\n"
        "# file. Rollback: schedule uninstall.\n"
        f"[Unit]\nDescription={title}\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={schedule.timeout_seconds}\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={root.replace('%', '%%')}\n"
        f"ExecStart={_quote(python)} -m {NODE_MODULE} {'drill' if drill else 'read'}\n"
        f"StandardOutput=append:{state}/{log_stem}.out.log\n"
        f"StandardError=append:{state}/{log_stem}.err.log\n"
        f"SyslogIdentifier={unit_name}\n"
    )
    timer = (
        f"# {cadence.description}\n"
        f"[Unit]\nDescription={tick}\n"
        f"\n[Timer]\nOnCalendar={cadence.on_calendar}\n"
        f"RandomizedDelaySec={cadence.randomized_delay}\n"
        f"Persistent={'true' if cadence.persistent else 'false'}\n"
        f"Unit={unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


class DevDeployReadingScheduler:
    """Check, install, remove and run the timers on one host."""

    def __init__(
        self,
        *,
        schedule: ModelDevDeployReadingSchedule,
        overlay: ModelDevDeployReadingOverlay,
        home: Path,
        run: Runner = run_command,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.schedule = schedule
        self.overlay = overlay
        self.home = home
        self.run = run
        self.environ = environ or {}
        units = overlay.schedule_units
        self.service_name = f"{units.unit_name}.service"
        self.timer_name = f"{units.unit_name}.timer"
        self.unit_dir = home / ".config/systemd/user"
        drill = schedule.drill
        self.drill_service_name = f"{units.drill_unit_name}.service" if drill else None
        self.drill_timer_name = f"{units.drill_unit_name}.timer" if drill else None

    def check(self) -> ModelScheduleResult:
        if not self.overlay.schedule_hosts:
            return ModelScheduleResult(
                exit_code=1, stderr="GAP no schedule host is declared\n"
            )
        names = ", ".join(host.name for host in self.overlay.schedule_hosts)
        return ModelScheduleResult(exit_code=0, stdout=f"OK schedule hosts: {names}\n")

    def _declared(self, host: str) -> ModelScheduleResult | None:
        if any(row.name == host for row in self.overlay.schedule_hosts):
            return None
        return ModelScheduleResult(
            exit_code=1,
            stderr=f"{host}: not in the {NODE_NAME} overlay's schedule_hosts, so the schedule is not declared for it\n",
        )

    def install(
        self, host: str, *, omni_root: Path, overlay_root: Path, dry_run: bool = False
    ) -> ModelScheduleResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        declared = next(row for row in self.overlay.schedule_hosts if row.name == host)
        venv = omni_root / declared.venv
        python = venv / "bin" / "python"
        units: list[tuple[str, str]] = []
        timers: list[str] = [self.timer_name]
        try:
            service, timer = render_units(
                self.schedule,
                self.overlay,
                omni_root=omni_root,
                venv=venv,
                home=self.home,
                overlay_root=overlay_root,
            )
            refuse_worktree_roots({self.service_name: service, self.timer_name: timer})
            units += [(self.service_name, service), (self.timer_name, timer)]
            if self.drill_timer_name and self.drill_service_name:
                drill_service, drill_timer = render_units(
                    self.schedule,
                    self.overlay,
                    omni_root=omni_root,
                    venv=venv,
                    home=self.home,
                    overlay_root=overlay_root,
                    drill=True,
                )
                refuse_worktree_roots(
                    {
                        self.drill_service_name: drill_service,
                        self.drill_timer_name: drill_timer,
                    }
                )
                units += [
                    (self.drill_service_name, drill_service),
                    (self.drill_timer_name, drill_timer),
                ]
                timers.append(self.drill_timer_name)
        except ValueError as exc:
            return ModelScheduleResult(exit_code=1, stderr=f"{exc}\n")
        if dry_run:
            return ModelScheduleResult(
                exit_code=0, stdout="".join(f"{text}\n" for _, text in units)
            )
        if not python.is_file():
            return ModelScheduleResult(
                exit_code=1, stderr=f"no venv interpreter at {python}\n"
            )
        started = self.run([str(python), "-c", f"import {NODE_MODULE}.__main__"])
        if started.returncode:
            return ModelScheduleResult(
                exit_code=1,
                stderr=(
                    f"{python} cannot import {NODE_MODULE}.__main__ (exit {started.returncode}); "
                    f"the scheduled run would not start\n{started.stderr}"
                ),
            )
        (omni_root / self.schedule.state_directory).mkdir(parents=True, exist_ok=True)
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        output = ""
        for name, text in units:
            target = self.unit_dir / name
            if target.exists() and target.read_text(encoding="utf-8") == text:
                output += f"{name} unchanged\n"
            else:
                target.write_text(text, encoding="utf-8")
                output += f"wrote {target}\n"
        steps = [["daemon-reload"]] + [["enable", "--now", name] for name in timers]
        for args in steps:
            done = self.run(["systemctl", "--user", *args])
            output += done.stdout
            if done.returncode:
                return ModelScheduleResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        output += f"installed {self.timer_name}: {self.schedule.cadence.description}\n"
        if self.schedule.drill is not None and len(timers) > 1:
            output += (
                f"installed {self.drill_timer_name}: "
                f"{self.schedule.drill.cadence.description}\n"
            )
        return ModelScheduleResult(exit_code=0, stdout=output)

    def _all_timers(self) -> list[str]:
        return [self.timer_name] + (
            [self.drill_timer_name] if self.drill_timer_name else []
        )

    def uninstall(self, host: str) -> ModelScheduleResult:
        """The rollback: stop and disable the timers, remove their unit files."""
        refused = self._declared(host)
        if refused is not None:
            return refused
        output = ""
        names = [self.timer_name, self.service_name]
        if self.drill_timer_name and self.drill_service_name:
            names += [self.drill_timer_name, self.drill_service_name]
        for timer in self._all_timers():
            if timer == self.timer_name or (self.unit_dir / timer).exists():
                done = self.run(["systemctl", "--user", "disable", "--now", timer])
                output += done.stdout
        for name in names:
            target = self.unit_dir / name
            if target.exists():
                target.unlink()
                output += f"removed {target}\n"
        reloaded = self.run(["systemctl", "--user", "daemon-reload"])
        output += reloaded.stdout
        if reloaded.returncode:
            return ModelScheduleResult(
                exit_code=reloaded.returncode, stdout=output, stderr=reloaded.stderr
            )
        return ModelScheduleResult(
            exit_code=0, stdout=output + f"uninstalled {self.timer_name}\n"
        )

    def run_now(self, host: str, *, drill: bool = False) -> ModelScheduleResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        if drill and self.drill_service_name is None:
            return ModelScheduleResult(
                exit_code=1, stderr="the contract declares no drill schedule\n"
            )
        service = self.drill_service_name if drill else self.service_name
        started = self.run(["systemctl", "--user", "start", str(service)])
        return ModelScheduleResult(
            exit_code=started.returncode,
            stdout=started.stdout or f"ran {service} once\n",
            stderr=started.stderr,
        )

    def status(self, host: str) -> ModelScheduleResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        enabled = self.run(["systemctl", "--user", "is-enabled", self.timer_name])
        timers = self.run(
            ["systemctl", "--user", "list-timers", *self._all_timers(), "--no-pager"]
        )
        return ModelScheduleResult(
            exit_code=0 if enabled.returncode == 0 else 1,
            stdout=f"is-enabled: {enabled.stdout.strip() or enabled.stderr.strip()}\n{timers.stdout}",
        )
