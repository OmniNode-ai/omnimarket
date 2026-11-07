# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's nightly schedule: render, install, uninstall, read and run it on a declared host.

The cadence is the contract's ``schedule`` block; the hosts, their venv and the unit environment
are the overlay's. ``uninstall`` is the rollback: it stops the timer and removes its unit files,
and changes nothing else.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.rules_unit_roots import refuse_worktree_roots
from omnimarket.nodes.node_code_graph_effect.overlay import (
    NODE_NAME,
    ModelCodeGraphOverlay,
    ModelCodeGraphScheduleHost,
)
from omnimarket.nodes.node_host_reading_effect.handlers.schedule import (
    ModelHostScheduleResult,
    Runner,
    run_command,
)

NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")


class ModelCodeGraphCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    on_calendar: str
    randomized_delay: str
    persistent: bool
    description: str


class ModelCodeGraphDrill(BaseModel):
    """The scheduled drill: the fault a run injects, so the error event of piece 1 stays inside its window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    fault: str = Field(min_length=1)
    cadence: ModelCodeGraphCadence


class ModelCodeGraphSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    state_directory: str
    timeout_seconds: int = Field(ge=1)
    cadence: ModelCodeGraphCadence
    drill: ModelCodeGraphDrill | None = None


def load_schedule(path: Path = CONTRACT_PATH) -> ModelCodeGraphSchedule:
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelCodeGraphSchedule.model_validate(contract["schedule"])


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


def render_systemd(
    schedule: ModelCodeGraphSchedule,
    overlay: ModelCodeGraphOverlay,
    host: ModelCodeGraphScheduleHost,
    *,
    omni_root: Path,
    home: Path,
    overlay_root: Path,
    drill: bool = False,
) -> tuple[str, str]:
    """The systemd user service and timer text of the nightly run, or of the scheduled drill."""
    if drill and schedule.drill is None:
        raise ValueError("the contract's schedule declares no drill")
    root = _safe(omni_root, "registry root")
    user_home = _safe(home, "home")
    venv = Path(host.venv) if Path(host.venv).is_absolute() else omni_root / host.venv
    python = _safe(venv / "bin" / "python", "venv interpreter")
    state = _safe(omni_root / schedule.state_directory, "state directory")
    environment = {
        "HOME": user_home,
        "OMNI_HOME": root,
        OVERLAY_ROOTS_ENV: _safe(overlay_root, "overlay root"),
        "PATH": f"{user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "ONEX_CODE_GRAPH_STATE_DIR": state,
        **{
            name: value.replace("$OMNI_HOME", root).replace("$HOME", user_home)
            for name, value in overlay.unit_environment.items()
        },
    }
    if drill and schedule.drill is not None:
        cadence = schedule.drill.cadence
        unit_name = schedule.drill.unit_name
        arguments = f"nightly --fault {schedule.drill.fault}"
        what = "ONEX scheduled code graph drill (piece 1 error event; an injected fault, drill=yes)"
        tick = "ONEX code graph drill tick"
        logs = "code-graph-drill"
    else:
        cadence = schedule.cadence
        unit_name = schedule.unit_name
        arguments = "nightly"
        what = "ONEX nightly code graph (piece 1 reading)"
        tick = "ONEX nightly code graph tick"
        logs = "code-graph"
    service = (
        "# Rendered by the code graph node (schedule install); the node contract owns the\n"
        "# cadence, so change the contract and install again rather than editing this file.\n"
        "# Rollback: schedule uninstall.\n"
        f"[Unit]\nDescription={what}\n"
        "\n[Service]\nType=oneshot\nNice=10\n"
        # The drill's injected fault makes the run exit 1 on purpose: that is the typed failure it exists to write.
        + ("SuccessExitStatus=1\n" if drill else "")
        + f"TimeoutStartSec={schedule.timeout_seconds}\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={root}\n"
        f"ExecStart={python} -m {NODE_MODULE} {arguments}\n"
        f"StandardOutput=append:{state}/{logs}.out.log\n"
        f"StandardError=append:{state}/{logs}.err.log\n"
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


class CodeGraphScheduler:
    """Install, remove, read and run the nightly schedule on one declared host."""

    def __init__(
        self,
        *,
        schedule: ModelCodeGraphSchedule,
        overlay: ModelCodeGraphOverlay,
        home: Path,
        run: Runner = run_command,
    ) -> None:
        self.schedule = schedule
        self.overlay = overlay
        self.home = home
        self.run = run
        self.service_name = f"{schedule.unit_name}.service"
        self.timer_name = f"{schedule.unit_name}.timer"
        drill = schedule.drill
        self.drill_service_name = f"{drill.unit_name}.service" if drill else None
        self.drill_timer_name = f"{drill.unit_name}.timer" if drill else None
        self.unit_dir = home / ".config/systemd/user"

    def _declared(
        self, name: str
    ) -> ModelCodeGraphScheduleHost | ModelHostScheduleResult:
        host = self.overlay.host(name)
        if host is not None:
            return host
        return ModelHostScheduleResult(
            exit_code=1,
            stderr=f"{name}: not in the {NODE_NAME} overlay's schedule_hosts\n",
        )

    def install(
        self, name: str, *, omni_root: Path, overlay_root: Path, dry_run: bool = False
    ) -> ModelHostScheduleResult:
        host = self._declared(name)
        if isinstance(host, ModelHostScheduleResult):
            return host
        try:
            service, timer = render_systemd(
                self.schedule,
                self.overlay,
                host,
                omni_root=omni_root,
                home=self.home,
                overlay_root=overlay_root,
            )
            units = {self.service_name: service, self.timer_name: timer}
            if self.drill_service_name and self.drill_timer_name:
                drill_service, drill_timer = render_systemd(
                    self.schedule,
                    self.overlay,
                    host,
                    omni_root=omni_root,
                    home=self.home,
                    overlay_root=overlay_root,
                    drill=True,
                )
                units[self.drill_service_name] = drill_service
                units[self.drill_timer_name] = drill_timer
            refuse_worktree_roots(units)
        except ValueError as exc:
            return ModelHostScheduleResult(exit_code=1, stderr=f"{exc}\n")
        files = [(self.unit_dir / name, body) for name, body in units.items()]
        if dry_run:
            return ModelHostScheduleResult(
                exit_code=0, stdout="".join(body + "\n" for _, body in files)
            )
        venv = (
            Path(host.venv) if Path(host.venv).is_absolute() else omni_root / host.venv
        )
        python = venv / "bin" / "python"
        if not python.is_file():
            return ModelHostScheduleResult(
                exit_code=1, stderr=f"no venv interpreter at {python}\n"
            )
        started = self.run([str(python), "-c", f"import {NODE_MODULE}.__main__"])
        if started.returncode:
            return ModelHostScheduleResult(
                exit_code=1,
                stderr=(
                    f"{python} cannot import {NODE_MODULE}.__main__ (exit {started.returncode}); "
                    f"the nightly run would not start\n{started.stderr}"
                ),
            )
        (omni_root / self.schedule.state_directory).mkdir(parents=True, exist_ok=True)
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
            *(
                ["systemctl", "--user", "enable", "--now", name]
                for name in self._timer_names()
            ),
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
        """The rollback: stop the timer and remove its unit files."""
        host = self._declared(name)
        if isinstance(host, ModelHostScheduleResult):
            return host
        done = self.run(
            ["systemctl", "--user", "disable", "--now", *self._timer_names()]
        )
        output = done.stdout
        for unit in (
            *self._timer_names(),
            self.service_name,
            *([self.drill_service_name] if self.drill_service_name else []),
        ):
            target = self.unit_dir / unit
            if target.exists():
                target.unlink()
                output += f"removed {target}\n"
        reloaded = self.run(["systemctl", "--user", "daemon-reload"])
        return ModelHostScheduleResult(
            exit_code=reloaded.returncode,
            stdout=output + reloaded.stdout,
            stderr=reloaded.stderr,
        )

    def _timer_names(self) -> tuple[str, ...]:
        return (
            self.timer_name,
            *([self.drill_timer_name] if self.drill_timer_name else []),
        )

    def run_now(self, name: str, *, drill: bool = False) -> ModelHostScheduleResult:
        host = self._declared(name)
        if isinstance(host, ModelHostScheduleResult):
            return host
        if drill and self.drill_service_name is None:
            return ModelHostScheduleResult(
                exit_code=1, stderr="the contract's schedule declares no drill\n"
            )
        unit = (
            self.drill_service_name
            if drill and self.drill_service_name
            else self.service_name
        )
        done = self.run(["systemctl", "--user", "start", "--no-block", unit])
        return ModelHostScheduleResult(
            exit_code=done.returncode,
            stdout=done.stdout
            or f"started the {'drill' if drill else 'nightly'} run on {name} once\n",
            stderr=done.stderr,
        )

    def status(self, name: str) -> ModelHostScheduleResult:
        host = self._declared(name)
        if isinstance(host, ModelHostScheduleResult):
            return host
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
        return ModelHostScheduleResult(
            exit_code=0 if done.returncode == 0 else 1,
            stdout=done.stdout or done.stderr,
        )


def state_directory(environ: dict[str, str] | None = None) -> Path | None:
    """Where the installed unit keeps the latest full result, when it is set."""
    env = os.environ if environ is None else environ
    value = env.get("ONEX_CODE_GRAPH_STATE_DIR", "").strip()
    return Path(value) if value else None
