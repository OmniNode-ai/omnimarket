# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The validator estate reading's own daily schedule: render, check, install, uninstall and run it on a host.

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
from omnimarket.nodes.node_validator_estate_reading_effect.overlay import (
    NODE_NAME,
    ModelEstateOverlay,
)

NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
#: The timer fires once a day, so the row must stay fresh for longer than this.
DAILY_PERIOD_SECONDS = 86400
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ModelEstateCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    on_calendar: str
    randomized_delay: str
    persistent: bool
    description: str


class ModelEstateSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    state_directory: str
    timeout_seconds: int = Field(ge=1)
    cadence: ModelEstateCadence
    schedulers: dict[str, str]


class ModelEstateScheduleResult(BaseModel):
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


def load_schedule(path: Path = CONTRACT_PATH) -> ModelEstateSchedule:
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelEstateSchedule.model_validate(contract["schedule"])


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
    schedule: ModelEstateSchedule,
    overlay: ModelEstateOverlay,
    *,
    omni_root: Path,
    venv: Path,
    home: Path,
    overlay_root: Path,
) -> tuple[str, str]:
    """The systemd user service and timer text."""
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
    cadence = schedule.cadence
    service = (
        "# Rendered by the validator estate reading node (schedule install); the node contract owns the\n"
        "# cadence, so change the contract and install again rather than editing this file.\n"
        "# Rollback: schedule uninstall.\n"
        "[Unit]\nDescription=ONEX daily validator estate read (reading rows only)\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={schedule.timeout_seconds}\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={root.replace('%', '%%')}\n"
        f"ExecStart={_quote(python)} -m {NODE_MODULE} read\n"
        f"StandardOutput=append:{state}/validator-estate.out.log\n"
        f"StandardError=append:{state}/validator-estate.err.log\n"
        f"SyslogIdentifier={schedule.unit_name}\n"
    )
    timer = (
        f"# {cadence.description}\n"
        "[Unit]\nDescription=ONEX daily validator estate read tick\n"
        f"\n[Timer]\nOnCalendar={cadence.on_calendar}\n"
        f"RandomizedDelaySec={cadence.randomized_delay}\n"
        f"Persistent={'true' if cadence.persistent else 'false'}\n"
        f"Unit={schedule.unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


class EstateScheduler:
    """Check, install, remove and run the daily timer on one host."""

    def __init__(
        self,
        *,
        schedule: ModelEstateSchedule,
        overlay: ModelEstateOverlay,
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

    def check(self) -> ModelEstateScheduleResult:
        if not self.overlay.schedule_hosts:
            return ModelEstateScheduleResult(
                exit_code=1, stderr="GAP no schedule host is declared\n"
            )
        max_age = self.overlay.publish.publish_max_age_seconds
        if max_age <= DAILY_PERIOD_SECONDS:
            return ModelEstateScheduleResult(
                exit_code=1,
                stderr=(
                    f"GAP the row stays fresh for {max_age} s but the timer reads once a day "
                    f"({DAILY_PERIOD_SECONDS} s), so the floor line would read stale between reads\n"
                ),
            )
        names = ", ".join(host.name for host in self.overlay.schedule_hosts)
        return ModelEstateScheduleResult(
            exit_code=0,
            stdout=f"OK schedule hosts: {names}; row fresh for {max_age} s, read every {DAILY_PERIOD_SECONDS} s\n",
        )

    def _declared(self, host: str) -> ModelEstateScheduleResult | None:
        if any(row.name == host for row in self.overlay.schedule_hosts):
            return None
        return ModelEstateScheduleResult(
            exit_code=1,
            stderr=f"{host}: not in the {NODE_NAME} overlay's schedule_hosts, so the schedule is not declared for it\n",
        )

    def install(
        self, host: str, *, omni_root: Path, overlay_root: Path, dry_run: bool = False
    ) -> ModelEstateScheduleResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        declared = next(row for row in self.overlay.schedule_hosts if row.name == host)
        venv = omni_root / declared.venv
        python = venv / "bin" / "python"
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
        except ValueError as exc:
            return ModelEstateScheduleResult(exit_code=1, stderr=f"{exc}\n")
        if dry_run:
            return ModelEstateScheduleResult(exit_code=0, stdout=f"{service}\n{timer}")
        if not python.is_file():
            return ModelEstateScheduleResult(
                exit_code=1, stderr=f"no venv interpreter at {python}\n"
            )
        started = self.run([str(python), "-c", f"import {NODE_MODULE}.__main__"])
        if started.returncode:
            return ModelEstateScheduleResult(
                exit_code=1,
                stderr=(
                    f"{python} cannot import {NODE_MODULE}.__main__ (exit {started.returncode}); "
                    f"the scheduled read would not start\n{started.stderr}"
                ),
            )
        (omni_root / self.schedule.state_directory).mkdir(parents=True, exist_ok=True)
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        output = ""
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
                return ModelEstateScheduleResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        return ModelEstateScheduleResult(
            exit_code=0,
            stdout=output
            + f"installed {self.timer_name}: {self.schedule.cadence.description}\n",
        )

    def uninstall(self, host: str) -> ModelEstateScheduleResult:
        """The rollback: stop and disable the timer, remove the two unit files."""
        refused = self._declared(host)
        if refused is not None:
            return refused
        output = ""
        done = self.run(["systemctl", "--user", "disable", "--now", self.timer_name])
        output += done.stdout
        for name in (self.timer_name, self.service_name):
            target = self.unit_dir / name
            if target.exists():
                target.unlink()
                output += f"removed {target}\n"
        reloaded = self.run(["systemctl", "--user", "daemon-reload"])
        output += reloaded.stdout
        if reloaded.returncode:
            return ModelEstateScheduleResult(
                exit_code=reloaded.returncode, stdout=output, stderr=reloaded.stderr
            )
        return ModelEstateScheduleResult(
            exit_code=0, stdout=output + f"uninstalled {self.timer_name}\n"
        )

    def run_now(self, host: str) -> ModelEstateScheduleResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        started = self.run(["systemctl", "--user", "start", self.service_name])
        return ModelEstateScheduleResult(
            exit_code=started.returncode,
            stdout=started.stdout or f"ran {self.service_name} once\n",
            stderr=started.stderr,
        )

    def status(self, host: str) -> ModelEstateScheduleResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        enabled = self.run(["systemctl", "--user", "is-enabled", self.timer_name])
        timers = self.run(
            ["systemctl", "--user", "list-timers", self.timer_name, "--no-pager"]
        )
        return ModelEstateScheduleResult(
            exit_code=0 if enabled.returncode == 0 else 1,
            stdout=f"is-enabled: {enabled.stdout.strip() or enabled.stderr.strip()}\n{timers.stdout}",
        )
