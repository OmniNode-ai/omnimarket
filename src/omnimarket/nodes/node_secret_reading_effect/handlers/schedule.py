# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The secret reading's own hourly schedule: render, check, install, uninstall and run it on a
host.

The cadence is the contract's ``schedule`` block and the hosts are the overlay's
``schedule_hosts``. The only scheduler is a systemd user timer, which survives a reboot when the
host's user lingers and the timer is ``Persistent``. ``install`` writes the two unit files and
enables the timer; ``uninstall`` is the rollback: it disables and stops the timer and removes the
two files, and it changes nothing else.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_secret_reading_effect.overlay import (
    NODE_NAME,
    OVERLAY_ID_ENV,
    ModelSecretReadingOverlay,
)

NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
JOIN_J4_OVERLAY_ID = "node_secret_reading_join_j4"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")
_SAFE_ID = re.compile(r"^[a-z0-9_]+$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ModelCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    on_calendar: str
    randomized_delay: str
    persistent: bool
    description: str


class ModelDrillSchedule(BaseModel):
    """The drill's own unit: the same read through the drill overlay (a made-up identity)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    cadence: ModelCadence


class ModelJoinJ4Schedule(BaseModel):
    """The J4 join's own unit (OMN-23085): the join test run on a cadence, so the floor holds its golden event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    cadence: ModelCadence


class ModelSecretReadingSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    state_directory: str
    timeout_seconds: int = Field(ge=1)
    cadence: ModelCadence
    schedulers: dict[str, str]
    #: Installed only where the overlay names a drill overlay root.
    drill: ModelDrillSchedule | None = None
    #: Installed only where the overlay root holds the J4 join overlay.
    join_j4: ModelJoinJ4Schedule | None = None


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


def load_schedule(path: Path = CONTRACT_PATH) -> ModelSecretReadingSchedule:
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelSecretReadingSchedule.model_validate(contract["schedule"])


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
    schedule: ModelSecretReadingSchedule,
    overlay: ModelSecretReadingOverlay,
    *,
    omni_root: Path,
    venv: Path,
    home: Path,
    overlay_root: Path,
    drill: bool = False,
    join_j4: bool = False,
    overlay_id: str = NODE_NAME,
) -> tuple[str, str]:
    """The systemd user service and timer text; ``drill`` and ``join_j4`` render those units' pairs.

    ``overlay_id`` is the overlay document the unit reads from ``overlay_root``; the unit pins it
    (``OVERLAY_ID_ENV``) when it is not the node's own name.
    """
    if not _SAFE_ID.match(overlay_id):
        raise ValueError(f"overlay id {overlay_id!r} is not a plain identifier")
    if drill and schedule.drill is None:
        raise ValueError("the contract declares no drill schedule")
    if join_j4 and schedule.join_j4 is None:
        raise ValueError("the contract declares no J4 join schedule")
    if join_j4 and schedule.join_j4 is not None:
        unit_name = schedule.join_j4.unit_name
        cadence = schedule.join_j4.cadence
        title = "ONEX J4 join test (overlays to secrets; resolves one key by overlay, scans for it, writes the join row)"
        log_stem = "secret-join-j4"
        tick = "ONEX J4 join test tick"
        exec_module = f"{NODE_MODULE}.runtime_join_j4 run"
    else:
        unit_name = (
            schedule.drill.unit_name if drill and schedule.drill else schedule.unit_name
        )
        cadence = (
            schedule.drill.cadence if drill and schedule.drill else schedule.cadence
        )
        title = (
            "ONEX daily secret reading drill (piece 5; a made-up identity, the error event)"
            if drill
            else "ONEX hourly secret reading (piece 5; read-only probes, reading rows only)"
        )
        log_stem = "secret-reading-drill" if drill else "secret-reading"
        tick = (
            "ONEX daily secret reading drill tick"
            if drill
            else "ONEX hourly secret reading tick"
        )
        exec_module = f"{NODE_MODULE} read"
    root = _safe(omni_root, "registry root")
    python = _safe(venv / "bin" / "python", "venv interpreter")
    user_home = _safe(home, "home")
    state = _safe(omni_root / schedule.state_directory, "state directory")
    environment = {
        "HOME": user_home,
        "OMNI_HOME": root,
        OVERLAY_ROOTS_ENV: _safe(overlay_root, "overlay root"),
        "PATH": f"{user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        **({OVERLAY_ID_ENV: overlay_id} if overlay_id != NODE_NAME else {}),
        **overlay.unit_environment,
    }
    service = (
        "# Rendered by the secret reading node (schedule install); the node contract owns\n"
        "# the cadence, so change the contract and install again rather than editing this\n"
        "# file. Rollback: schedule uninstall.\n"
        f"[Unit]\nDescription={title}\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={schedule.timeout_seconds}\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={root.replace('%', '%%')}\n"
        f"ExecStart={_quote(python)} -m {exec_module}\n"
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


class SecretReadingScheduler:
    """Check, install, remove and run the hourly timer on one host."""

    def __init__(
        self,
        *,
        schedule: ModelSecretReadingSchedule,
        overlay: ModelSecretReadingOverlay,
        home: Path,
        run: Runner = run_command,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.schedule = schedule
        self.overlay = overlay
        self.home = home
        self.run = run
        self.environ = environ or {}
        self.service_name = f"{schedule.unit_name}.service"
        self.timer_name = f"{schedule.unit_name}.timer"
        self.unit_dir = home / ".config/systemd/user"
        drill = schedule.drill
        self.drill_service_name = f"{drill.unit_name}.service" if drill else None
        self.drill_timer_name = f"{drill.unit_name}.timer" if drill else None
        join_j4 = schedule.join_j4
        self.join_j4_service_name = f"{join_j4.unit_name}.service" if join_j4 else None
        self.join_j4_timer_name = f"{join_j4.unit_name}.timer" if join_j4 else None

    def _drill_root(self) -> Path | None:
        """The drill overlay root the overlay names, when the contract declares a drill."""
        if self.schedule.drill is None or self.overlay.drill_overlay_root is None:
            return None
        return Path(
            os.path.expandvars(os.path.expanduser(self.overlay.drill_overlay_root))
        )

    def _drill_id(self) -> str | None:
        """The drill's overlay document id, read from the same overlay root as the hourly unit."""
        if self.schedule.drill is None:
            return None
        return self.overlay.drill_overlay_id

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
        self,
        host: str,
        *,
        omni_root: Path,
        overlay_root: Path,
        dry_run: bool = False,
        overlay_id: str = NODE_NAME,
    ) -> ModelScheduleResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        declared = next(row for row in self.overlay.schedule_hosts if row.name == host)
        venv = omni_root / declared.venv
        python = venv / "bin" / "python"
        drill_root = self._drill_root()
        drill_id = self._drill_id()
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
                overlay_id=overlay_id,
            )
            units += [(self.service_name, service), (self.timer_name, timer)]
            if (
                (drill_root is not None or drill_id is not None)
                and self.drill_timer_name
                and self.drill_service_name
            ):
                drill_service, drill_timer = render_units(
                    self.schedule,
                    self.overlay,
                    omni_root=omni_root,
                    venv=venv,
                    home=self.home,
                    overlay_root=drill_root or overlay_root,
                    drill=True,
                    overlay_id=drill_id or NODE_NAME,
                )
                units += [
                    (self.drill_service_name, drill_service),
                    (self.drill_timer_name, drill_timer),
                ]
                timers.append(self.drill_timer_name)
            if (
                self.join_j4_service_name
                and self.join_j4_timer_name
                and (overlay_root / JOIN_J4_OVERLAY_ID / "overlay.yaml").is_file()
            ):
                j4_service, j4_timer = render_units(
                    self.schedule,
                    self.overlay,
                    omni_root=omni_root,
                    venv=venv,
                    home=self.home,
                    overlay_root=overlay_root,
                    join_j4=True,
                    overlay_id=overlay_id,
                )
                units += [
                    (self.join_j4_service_name, j4_service),
                    (self.join_j4_timer_name, j4_timer),
                ]
                timers.append(self.join_j4_timer_name)
        except ValueError as exc:
            return ModelScheduleResult(exit_code=1, stderr=f"{exc}\n")
        if dry_run:
            return ModelScheduleResult(
                exit_code=0, stdout="".join(f"{text}\n" for _, text in units)
            )
        drill_file = (
            drill_root / NODE_NAME / "overlay.yaml"
            if drill_root is not None
            else overlay_root / drill_id / "overlay.yaml"
            if drill_id is not None
            else None
        )
        if drill_file is not None and not drill_file.is_file():
            return ModelScheduleResult(
                exit_code=1, stderr=f"no drill overlay at {drill_file}\n"
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
                    f"the hourly run would not start\n{started.stderr}"
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
        if self.schedule.drill is not None and self.drill_timer_name in timers:
            output += (
                f"installed {self.drill_timer_name}: "
                f"{self.schedule.drill.cadence.description}\n"
            )
        if self.schedule.join_j4 is not None and self.join_j4_timer_name in timers:
            output += (
                f"installed {self.join_j4_timer_name}: "
                f"{self.schedule.join_j4.cadence.description}\n"
            )
        return ModelScheduleResult(exit_code=0, stdout=output)

    def _all_timers(self) -> list[str]:
        return (
            [self.timer_name]
            + ([self.drill_timer_name] if self.drill_timer_name else [])
            + ([self.join_j4_timer_name] if self.join_j4_timer_name else [])
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
        if self.join_j4_timer_name and self.join_j4_service_name:
            names += [self.join_j4_timer_name, self.join_j4_service_name]
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

    def run_now(
        self, host: str, *, drill: bool = False, join_j4: bool = False
    ) -> ModelScheduleResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        if drill and join_j4:
            return ModelScheduleResult(
                exit_code=1, stderr="run the drill unit or the J4 unit, not both\n"
            )
        if drill and self.drill_service_name is None:
            return ModelScheduleResult(
                exit_code=1, stderr="the contract declares no drill schedule\n"
            )
        if join_j4 and self.join_j4_service_name is None:
            return ModelScheduleResult(
                exit_code=1, stderr="the contract declares no J4 join schedule\n"
            )
        service = (
            self.join_j4_service_name
            if join_j4
            else (self.drill_service_name if drill else self.service_name)
        )
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
