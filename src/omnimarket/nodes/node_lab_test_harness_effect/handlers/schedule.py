# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The harness's own timer: render, check, install, uninstall, run and read it on a declared host.

The interval is the overlay's ``schedule`` block and the hosts and the unit environment are the
overlay's ``schedule_hosts`` and ``unit_environment``; the contract's ``schedule`` block holds only
how the unit is run. The only scheduler is a systemd user timer, which survives a reboot when the
host's user lingers and the timer is ``Persistent``. ``install`` writes the two unit files and
enables the timer; ``uninstall`` is the rollback: it disables and stops the timer and removes the
two files, and it changes nothing else.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.rules_unit_roots import refuse_worktree_roots
from omnimarket.models.lab_test_harness import ModelLabTestHarnessOverlay

NODE_NAME = "node_lab_test_harness_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
RUNTIME_MODULE = f"{NODE_MODULE}.runtime_lab_test_harness"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ModelHarnessUnit(BaseModel):
    """The contract's ``schedule`` block: how the unit is run, never when."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    state_directory: str
    timeout_seconds: int = Field(ge=1)
    boot_delay: str
    randomized_delay: str
    persistent: bool
    schedulers: dict[str, str]


class ModelHarnessScheduleResult(BaseModel):
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


def load_unit(path: Path = CONTRACT_PATH) -> ModelHarnessUnit:
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelHarnessUnit.model_validate(contract["schedule"])


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
    unit: ModelHarnessUnit,
    overlay: ModelLabTestHarnessOverlay,
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
    state = _safe(omni_root / unit.state_directory, "state directory")
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
    interval = overlay.schedule.interval_seconds
    description = f"every {interval} s, the first run {unit.boot_delay} after boot"
    service = (
        "# Rendered by the central test harness node (schedule install); the overlay owns the\n"
        "# interval, so change the overlay and install again rather than editing this file.\n"
        "# Rollback: schedule uninstall.\n"
        "[Unit]\nDescription=ONEX central test harness (golden and error cases on the lab)\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={unit.timeout_seconds}\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={root.replace('%', '%%')}\n"
        f"ExecStart={_quote(python)} -m {RUNTIME_MODULE} "
        f"--omni-home {_quote(root)} --state-dir {_quote(state)}\n"
        f"StandardOutput=append:{state}/harness.out.log\n"
        f"StandardError=append:{state}/harness.err.log\n"
        f"SyslogIdentifier={unit.unit_name}\n"
    )
    timer = (
        f"# {description}\n"
        "[Unit]\nDescription=ONEX central test harness tick\n"
        f"\n[Timer]\nOnBootSec={unit.boot_delay}\n"
        f"OnUnitActiveSec={interval}s\n"
        f"RandomizedDelaySec={unit.randomized_delay}\n"
        f"Persistent={'true' if unit.persistent else 'false'}\n"
        f"Unit={unit.unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


class HarnessScheduler:
    """Check, install, remove and run the harness timer on one host."""

    def __init__(
        self,
        *,
        unit: ModelHarnessUnit,
        overlay: ModelLabTestHarnessOverlay,
        home: Path,
        run: Runner = run_command,
    ) -> None:
        self.unit = unit
        self.overlay = overlay
        self.home = home
        self.run = run
        self.service_name = f"{unit.unit_name}.service"
        self.timer_name = f"{unit.unit_name}.timer"
        self.unit_dir = home / ".config/systemd/user"

    def check(self) -> ModelHarnessScheduleResult:
        if not self.overlay.schedule_hosts:
            return ModelHarnessScheduleResult(
                exit_code=1, stderr="GAP no schedule host is declared\n"
            )
        names = ", ".join(host.name for host in self.overlay.schedule_hosts)
        return ModelHarnessScheduleResult(
            exit_code=0, stdout=f"OK schedule hosts: {names}\n"
        )

    def _declared(self, host: str) -> ModelHarnessScheduleResult | None:
        if any(row.name == host for row in self.overlay.schedule_hosts):
            return None
        return ModelHarnessScheduleResult(
            exit_code=1,
            stderr=f"{host}: not in the {NODE_NAME} overlay's schedule_hosts, so the schedule is not declared for it\n",
        )

    def install(
        self, host: str, *, omni_root: Path, overlay_root: Path, dry_run: bool = False
    ) -> ModelHarnessScheduleResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        declared = next(row for row in self.overlay.schedule_hosts if row.name == host)
        venv = omni_root / declared.venv
        python = venv / "bin" / "python"
        try:
            service, timer = render_units(
                self.unit,
                self.overlay,
                omni_root=omni_root,
                venv=venv,
                home=self.home,
                overlay_root=overlay_root,
            )
            refuse_worktree_roots({self.service_name: service, self.timer_name: timer})
        except ValueError as exc:
            return ModelHarnessScheduleResult(exit_code=1, stderr=f"{exc}\n")
        if dry_run:
            return ModelHarnessScheduleResult(exit_code=0, stdout=f"{service}\n{timer}")
        if not python.is_file():
            return ModelHarnessScheduleResult(
                exit_code=1, stderr=f"no venv interpreter at {python}\n"
            )
        started = self.run([str(python), "-c", f"import {RUNTIME_MODULE}"])
        if started.returncode:
            return ModelHarnessScheduleResult(
                exit_code=1,
                stderr=(
                    f"{python} cannot import {RUNTIME_MODULE} (exit {started.returncode}); "
                    f"the harness run would not start\n{started.stderr}"
                ),
            )
        (omni_root / self.unit.state_directory).mkdir(parents=True, exist_ok=True)
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
                return ModelHarnessScheduleResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        return ModelHarnessScheduleResult(
            exit_code=0,
            stdout=output
            + f"installed {self.timer_name}: every {self.overlay.schedule.interval_seconds} s\n",
        )

    def uninstall(self, host: str) -> ModelHarnessScheduleResult:
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
            return ModelHarnessScheduleResult(
                exit_code=reloaded.returncode, stdout=output, stderr=reloaded.stderr
            )
        return ModelHarnessScheduleResult(
            exit_code=0, stdout=output + f"uninstalled {self.timer_name}\n"
        )

    def run_now(self, host: str) -> ModelHarnessScheduleResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        started = self.run(["systemctl", "--user", "start", self.service_name])
        return ModelHarnessScheduleResult(
            exit_code=started.returncode,
            stdout=started.stdout or f"ran {self.service_name} once\n",
            stderr=started.stderr,
        )

    def status(self, host: str) -> ModelHarnessScheduleResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        enabled = self.run(["systemctl", "--user", "is-enabled", self.timer_name])
        timers = self.run(
            ["systemctl", "--user", "list-timers", self.timer_name, "--no-pager"]
        )
        return ModelHarnessScheduleResult(
            exit_code=0 if enabled.returncode == 0 else 1,
            stdout=f"is-enabled: {enabled.stdout.strip() or enabled.stderr.strip()}\n{timers.stdout}",
        )
