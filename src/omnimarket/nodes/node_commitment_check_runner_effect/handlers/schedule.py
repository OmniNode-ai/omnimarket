# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The check runner's schedule: render, install, uninstall and run it on a declared lab host.

The interval is the contract's ``schedule`` block; the hosts and their unit environment are the
overlay's ``schedule_hosts``. A lab host gets a systemd user timer. ``uninstall`` is the rollback:
it stops the schedule and removes its unit files, and it changes nothing else.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_commitment_check_runner_effect.models import (
    ModelCommitmentCheckHost,
    ModelCommitmentCheckRules,
)

NODE_NAME = "node_commitment_check_runner_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ModelCommitmentCheckSchedule(BaseModel):
    """The contract's ``schedule`` block: the unit's name, how often it ticks and how long a tick may run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    interval_seconds: int = Field(ge=1)
    timeout_seconds: int = Field(ge=1)
    state_directory: str
    boot_delay: str
    randomized_delay: str


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


def load_schedule(path: Path = CONTRACT_PATH) -> ModelCommitmentCheckSchedule:
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelCommitmentCheckSchedule.model_validate(contract["schedule"])


def _safe(path: Path, what: str) -> str:
    text = str(path)
    if not path.is_absolute():
        raise ValueError(
            f"{what} {text!r} is not an absolute path: a unit line resolves it from the unit's own directory"
        )
    if not _SAFE_PATH.match(text):
        raise ValueError(
            f"{what} {text!r} holds a character a unit line cannot carry unquoted"
        )
    return text


def _assignment(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{name}={escaped}"\n'


def render_systemd(
    schedule: ModelCommitmentCheckSchedule,
    host: ModelCommitmentCheckHost,
    *,
    omni_root: Path,
    home: Path,
    overlay_root: Path | None = None,
    overlay_roots: Sequence[Path] = (),
    overlay_pins: Mapping[str, Path] | None = None,
) -> tuple[str, str]:
    """The systemd user service and timer text.

    The unit carries the overlay configuration the installer validated: every overlay root (``overlay_roots``, or the
    one ``overlay_root``) and each pinned overlay file (``overlay_pins``, variable name to file), so a scheduled run
    reads the same overlays the install did.
    """
    roots = list(overlay_roots) or ([overlay_root] if overlay_root is not None else [])
    pins = dict(overlay_pins or {})
    if not roots and not pins:
        raise ValueError(
            "no overlay root or pinned overlay file to carry into the unit: the scheduled run would find no overlay"
        )
    root = _safe(omni_root, "registry root")
    user_home = _safe(home, "home")
    python = _safe(omni_root / host.venv / "bin" / "python", "venv interpreter")
    state = _safe(omni_root / schedule.state_directory, "state directory")
    environment = {
        "HOME": user_home,
        "OMNI_HOME": root,
        **(
            {
                OVERLAY_ROOTS_ENV: os.pathsep.join(
                    _safe(r, "overlay root") for r in roots
                )
            }
            if roots
            else {}
        ),
        **{name: _safe(path, f"{name} overlay file") for name, path in pins.items()},
        "PATH": f"{user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        **{
            name: value.replace("$OMNI_HOME", root).replace("$HOME", user_home)
            for name, value in host.environment.items()
        },
    }
    service = (
        "# Rendered by the commitment check runner node (schedule install); the node contract owns the\n"
        "# interval, so change the contract and install again rather than editing this file.\n"
        "# Rollback: schedule uninstall.\n"
        "[Unit]\nDescription=ONEX commitment check runner\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={schedule.timeout_seconds}\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={root}\n"
        f"ExecStart={python} -m {NODE_MODULE}\n"
        f"StandardOutput=append:{state}/commitment-check.out.log\n"
        f"StandardError=append:{state}/commitment-check.err.log\n"
        f"SyslogIdentifier={schedule.unit_name}\n"
    )
    timer = (
        f"# every {schedule.interval_seconds} s after the previous run began\n"
        "[Unit]\nDescription=ONEX commitment check runner tick\n"
        f"\n[Timer]\nOnBootSec={schedule.boot_delay}\n"
        f"OnUnitActiveSec={schedule.interval_seconds}s\n"
        f"RandomizedDelaySec={schedule.randomized_delay}\n"
        f"Unit={schedule.unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


class CommitmentCheckScheduler:
    """Install, remove, read and run the schedule on one declared host."""

    def __init__(
        self,
        *,
        schedule: ModelCommitmentCheckSchedule,
        overlay: ModelCommitmentCheckRules,
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

    def _declared(self, name: str) -> ModelCommitmentCheckHost | ModelScheduleResult:
        host = self.overlay.host(name)
        if host is not None:
            return host
        return ModelScheduleResult(
            exit_code=1,
            stderr=f"{name}: not in the {NODE_NAME} overlay's schedule_hosts\n",
        )

    def install(
        self,
        name: str,
        *,
        omni_root: Path,
        overlay_root: Path | None = None,
        overlay_roots: Sequence[Path] = (),
        overlay_pins: Mapping[str, Path] | None = None,
        dry_run: bool = False,
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
                overlay_roots=overlay_roots,
                overlay_pins=overlay_pins,
            )
        except ValueError as exc:
            return ModelScheduleResult(exit_code=1, stderr=f"{exc}\n")
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
                f"{started.returncode}); the timer run would not start\n",
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
            + f"installed on {host.name}: every {self.schedule.interval_seconds} s\n",
        )

    def uninstall(self, name: str) -> ModelScheduleResult:
        """The rollback: stop the schedule and remove its unit files."""
        host = self._declared(name)
        if isinstance(host, ModelScheduleResult):
            return host
        stopped = self.run(["systemctl", "--user", "disable", "--now", self.timer_name])
        if stopped.returncode and not any(
            absent in stopped.stderr.lower()
            for absent in ("does not exist", "not loaded", "no such file")
        ):
            # The timer may still be running: keep its unit files so the rollback can be run again.
            return ModelScheduleResult(
                exit_code=stopped.returncode,
                stderr=f"the timer was not stopped: {stopped.stderr}",
            )
        output = ""
        for target in (
            self.unit_dir / self.timer_name,
            self.unit_dir / self.service_name,
        ):
            if target.exists():
                target.unlink()
                output += f"removed {target}\n"
        reloaded = self.run(["systemctl", "--user", "daemon-reload"])
        if reloaded.returncode:
            return ModelScheduleResult(
                exit_code=reloaded.returncode,
                stdout=output,
                stderr=f"the unit files were removed but daemon-reload failed: {reloaded.stderr}",
            )
        return ModelScheduleResult(
            exit_code=0, stdout=output + f"uninstalled on {name}\n"
        )

    def status(self, name: str) -> ModelScheduleResult:
        host = self._declared(name)
        if isinstance(host, ModelScheduleResult):
            return host
        done = self.run(
            ["systemctl", "--user", "list-timers", self.timer_name, "--all"]
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
    "CommitmentCheckScheduler",
    "ModelCommitmentCheckSchedule",
    "ModelScheduleResult",
    "load_schedule",
    "render_systemd",
]
