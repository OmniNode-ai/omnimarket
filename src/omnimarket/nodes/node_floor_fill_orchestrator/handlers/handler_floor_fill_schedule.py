# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerFloorFillSchedule: the fill's own tick unit, rendered, installed and removed by the node.

The unit runs the node through its runtime module, the census node's form: the dispatch venv's interpreter,
``-m omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator --overlay-json <J> --this-host <H> tick``,
on ``floor_fill.host_id`` every ``floor_fill.tick_interval_seconds``. ``J`` is the overlay's ``floor_fill`` section
as validated JSON and ``H`` is ``floor_fill.host_id``. The unit's name, deadline and dispatch venv are the contract's
``schedule`` block; the host and the interval are the overlay's. The host must be an available row of the lab host
table: any other value is refused naming ``floor_fill.host_id``. ``uninstall`` is the rollback: it stops the timer and
removes its unit files.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from omnimarket.handlers.rules_unit_roots import refuse_worktree_roots
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    FloorFillConfigError,
    ModelFloorFillConfig,
    ModelFloorFillSchedule,
    ModelFloorFillScheduleResult,
)

NODE_NAME = "node_floor_fill_orchestrator"
NODE_MODULE = (
    "omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator"
)
OVERLAY_SECTION = "floor_fill"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command), capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(command), 127, "", str(exc))


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
    """One systemd command-line word: quoted, with the backslash, quote, ``%`` and ``$`` escaped."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def overlay_json(config: ModelFloorFillConfig) -> str:
    """``J``: the overlay's ``floor_fill`` section, validated, as one compact JSON line."""
    return json.dumps(
        {OVERLAY_SECTION: config.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    )


def dispatch_python(schedule: ModelFloorFillSchedule, omni_root: Path) -> Path:
    """The dispatch venv's interpreter under the registry root: what the unit runs."""
    return omni_root / schedule.dispatch_venv / "bin" / "python"


def exec_start(
    schedule: ModelFloorFillSchedule, config: ModelFloorFillConfig, omni_root: Path
) -> str:
    """The unit's command line: the runtime module, the overlay as JSON, this host, and ``tick``."""
    python = _safe(dispatch_python(schedule, omni_root), "dispatch venv interpreter")
    return (
        f"{_quote(python)} -m {NODE_MODULE} --overlay-json {_quote(overlay_json(config))} "
        f"--this-host {_quote(config.host_id)} tick"
    )


def render_systemd(
    schedule: ModelFloorFillSchedule,
    config: ModelFloorFillConfig,
    *,
    omni_root: Path,
    home: Path,
) -> tuple[str, str]:
    """The systemd user service and timer text."""
    root = _safe(omni_root, "registry root")
    user_home = _safe(home, "home")
    environment = {
        "HOME": user_home,
        "OMNI_HOME": root,
        "PATH": f"{user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
    }
    interval = f"{config.tick_interval_seconds}s"
    service = (
        "# Rendered by the floor fill node (schedule install); the node contract owns the unit's name and\n"
        "# deadline and the overlay the interval, so change those and install again rather than editing this file.\n"
        "# Rollback: schedule uninstall.\n"
        "[Unit]\nDescription=ONEX floor fill tick (stopped-line fill)\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={schedule.timeout_seconds}\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={root}\n"
        f"ExecStart={exec_start(schedule, config, omni_root)}\n"
        f"SyslogIdentifier={schedule.unit_name}\n"
    )
    timer = (
        f"# floor fill tick every {interval} on {config.host_id}\n"
        "[Unit]\nDescription=ONEX floor fill tick timer\n"
        f"\n[Timer]\nOnBootSec={interval}\nOnUnitActiveSec={interval}\nAccuracySec=1s\n"
        f"Unit={schedule.unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


class HandlerFloorFillSchedule:
    """Install, remove, read and run the tick unit on the fill host."""

    def __init__(
        self,
        *,
        schedule: ModelFloorFillSchedule,
        config: ModelFloorFillConfig,
        hosts: Iterable[tuple[str, bool]],
        home: Path,
        run: Runner = run_command,
    ) -> None:
        self.schedule = schedule
        self.config = config
        self.hosts = tuple(hosts)
        self.home = home
        self.run = run
        self.service_name = f"{schedule.unit_name}.service"
        self.timer_name = f"{schedule.unit_name}.timer"
        self.unit_dir = home / ".config/systemd/user"

    def _refusal(self, this_host: str) -> ModelFloorFillScheduleResult | None:
        try:
            self.config.check_fill_host(self.hosts)
        except FloorFillConfigError as error:
            return ModelFloorFillScheduleResult(exit_code=1, stderr=f"{error}\n")
        if this_host != self.config.host_id:
            return ModelFloorFillScheduleResult(
                exit_code=1,
                stderr=(
                    f"{this_host}: the tick unit belongs on floor_fill.host_id "
                    f"{self.config.host_id!r}\n"
                ),
            )
        if self.schedule.timeout_seconds <= self.config.command_timeout_seconds:
            return ModelFloorFillScheduleResult(
                exit_code=1,
                stderr=(
                    f"the contract's schedule.timeout_seconds ({self.schedule.timeout_seconds}) "
                    "is not above floor_fill.command_timeout_seconds "
                    f"({self.config.command_timeout_seconds})\n"
                ),
            )
        return None

    def install(
        self,
        *,
        this_host: str,
        omni_root: Path,
        dry_run: bool = False,
    ) -> ModelFloorFillScheduleResult:
        refused = self._refusal(this_host)
        if refused is not None:
            return refused
        try:
            service, timer = render_systemd(
                self.schedule,
                self.config,
                omni_root=omni_root,
                home=self.home,
            )
            refuse_worktree_roots({self.service_name: service, self.timer_name: timer})
        except ValueError as exc:
            return ModelFloorFillScheduleResult(exit_code=1, stderr=f"{exc}\n")
        files = [
            (self.unit_dir / self.service_name, service),
            (self.unit_dir / self.timer_name, timer),
        ]
        if dry_run:
            return ModelFloorFillScheduleResult(
                exit_code=0, stdout="".join(body + "\n" for _, body in files)
            )
        python = dispatch_python(self.schedule, omni_root)
        if not python.is_file():
            return ModelFloorFillScheduleResult(
                exit_code=1, stderr=f"no dispatch venv interpreter at {python}\n"
            )
        started = self.run([str(python), "-c", f"import {NODE_MODULE}"])
        if started.returncode:
            return ModelFloorFillScheduleResult(
                exit_code=1,
                stderr=(
                    f"{python} cannot import {NODE_MODULE} (exit {started.returncode}); "
                    f"the tick would not start\n{started.stderr}"
                ),
            )
        output = ""
        for target, body in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.read_text(encoding="utf-8") == body:
                output += f"{target.name} unchanged\n"
            else:
                target.write_text(body, encoding="utf-8")
                output += f"wrote {target}\n"
        for step in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", self.timer_name],
        ):
            done = self.run(step)
            output += done.stdout
            if done.returncode:
                return ModelFloorFillScheduleResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        return ModelFloorFillScheduleResult(
            exit_code=0,
            stdout=output
            + f"installed on {self.config.host_id}: tick every {self.config.tick_interval_seconds}s\n",
        )

    def uninstall(self, *, this_host: str) -> ModelFloorFillScheduleResult:
        """The rollback: stop the timer and remove its unit files."""
        refused = self._refusal(this_host)
        if refused is not None:
            return refused
        done = self.run(["systemctl", "--user", "disable", "--now", self.timer_name])
        output = done.stdout
        for unit in (self.timer_name, self.service_name):
            target = self.unit_dir / unit
            if target.exists():
                target.unlink()
                output += f"removed {target}\n"
        reloaded = self.run(["systemctl", "--user", "daemon-reload"])
        return ModelFloorFillScheduleResult(
            exit_code=reloaded.returncode,
            stdout=output + reloaded.stdout,
            stderr=reloaded.stderr,
        )

    def run_now(self, *, this_host: str) -> ModelFloorFillScheduleResult:
        refused = self._refusal(this_host)
        if refused is not None:
            return refused
        done = self.run(
            ["systemctl", "--user", "start", "--no-block", self.service_name]
        )
        return ModelFloorFillScheduleResult(
            exit_code=done.returncode,
            stdout=done.stdout or f"started one tick on {self.config.host_id}\n",
            stderr=done.stderr,
        )

    def status(self, *, this_host: str) -> ModelFloorFillScheduleResult:
        refused = self._refusal(this_host)
        if refused is not None:
            return refused
        done = self.run(
            [
                "systemctl",
                "--user",
                "list-timers",
                self.timer_name,
                "--all",
                "--no-pager",
            ]
        )
        return ModelFloorFillScheduleResult(
            exit_code=0 if done.returncode == 0 else 1,
            stdout=done.stdout or done.stderr,
        )


__all__: list[str] = [
    "NODE_MODULE",
    "NODE_NAME",
    "HandlerFloorFillSchedule",
    "exec_start",
    "overlay_json",
    "render_systemd",
    "run_command",
]
