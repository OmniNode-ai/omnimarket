# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drill's own hourly timer: render, check, install, uninstall, run and read it on a declared host.

The cadence is the contract's ``schedule`` block and the hosts and the unit environment are the overlay's
``schedule_hosts`` and ``unit_environment``. The only scheduler is a systemd user timer, which survives a reboot when
the host's user lingers and the timer is ``Persistent``. ``install`` writes the two unit files and enables the timer;
``uninstall`` is the rollback: it disables and stops the timer and removes the two files, and it changes nothing else.
The unit's command is the node's own ``run`` verb.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from omnimarket.handlers.rules_unit_roots import refuse_worktree_roots
from omnimarket.nodes.node_reading_writer_effect.handlers.schedule import (
    ModelReadingSchedule,
    ModelScheduleResult,
    Runner,
    run_command,
)
from omnimarket.nodes.node_stop_drill_effect.models import ModelStopDrillOverlay

NODE_NAME = "node_stop_drill_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")


def load_schedule(path: Path = CONTRACT_PATH) -> ModelReadingSchedule:
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelReadingSchedule.model_validate(contract["schedule"])


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
    schedule: ModelReadingSchedule,
    overlay: ModelStopDrillOverlay,
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
        # The overlay names no home directory: its values start from these two, filled here.
        **{
            name: value.replace("$OMNI_HOME", root).replace("$HOME", user_home)
            for name, value in overlay.unit_environment.items()
        },
    }
    cadence = schedule.cadence
    service = (
        "# Rendered by the stop drill node (schedule install); the node contract owns the\n"
        "# cadence, so change the contract and install again rather than editing this file.\n"
        "# Rollback: schedule uninstall.\n"
        "[Unit]\nDescription=ONEX hourly stop drill (a lane sent above a stop on a test admission instance)\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={schedule.timeout_seconds}\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={root.replace('%', '%%')}\n"
        f"ExecStart={_quote(python)} -m {NODE_MODULE} run --trigger schedule --state-dir {_quote(state)}\n"
        f"StandardOutput=append:{state}/stop-drill.out.log\n"
        f"StandardError=append:{state}/stop-drill.err.log\n"
        f"SyslogIdentifier={schedule.unit_name}\n"
    )
    timer = (
        f"# {cadence.description}\n"
        "[Unit]\nDescription=ONEX hourly stop drill tick\n"
        f"\n[Timer]\nOnCalendar={cadence.on_calendar}\n"
        f"RandomizedDelaySec={cadence.randomized_delay}\n"
        f"Persistent={'true' if cadence.persistent else 'false'}\n"
        f"Unit={schedule.unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


class StopDrillScheduler:
    """Check, install, remove and run the hourly timer on one host."""

    def __init__(
        self,
        *,
        schedule: ModelReadingSchedule,
        overlay: ModelStopDrillOverlay,
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
        try:
            service, timer = render_units(
                self.schedule,
                self.overlay,
                omni_root=omni_root,
                venv=venv,
                home=self.home,
                overlay_root=overlay_root,
            )
            # A unit that runs from a worktree dies when the host reconcile removes the tree: refuse before anything
            # is written, naming the path (OMN-22735).
            refuse_worktree_roots({self.service_name: service, self.timer_name: timer})
        except ValueError as exc:
            return ModelScheduleResult(exit_code=1, stderr=f"{exc}\n")
        if dry_run:
            return ModelScheduleResult(exit_code=0, stdout=f"{service}\n{timer}")
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
                return ModelScheduleResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        return ModelScheduleResult(
            exit_code=0,
            stdout=output
            + f"installed {self.timer_name}: {self.schedule.cadence.description}\n",
        )

    def uninstall(self, host: str) -> ModelScheduleResult:
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
            return ModelScheduleResult(
                exit_code=reloaded.returncode, stdout=output, stderr=reloaded.stderr
            )
        return ModelScheduleResult(
            exit_code=0, stdout=output + f"uninstalled {self.timer_name}\n"
        )

    def run_now(self, host: str) -> ModelScheduleResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        started = self.run(["systemctl", "--user", "start", self.service_name])
        return ModelScheduleResult(
            exit_code=started.returncode,
            stdout=started.stdout or f"ran {self.service_name} once\n",
            stderr=started.stderr,
        )

    def status(self, host: str) -> ModelScheduleResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        enabled = self.run(["systemctl", "--user", "is-enabled", self.timer_name])
        timers = self.run(
            ["systemctl", "--user", "list-timers", self.timer_name, "--no-pager"]
        )
        return ModelScheduleResult(
            exit_code=0 if enabled.returncode == 0 else 1,
            stdout=f"is-enabled: {enabled.stdout.strip() or enabled.stderr.strip()}\n{timers.stdout}",
        )
