# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's daily schedule: render, install and uninstall a systemd user timer that runs it at 05:30 Eastern.

The cadence is the contract's ``schedule`` block and nothing else: change the contract and install again rather than
editing a unit file. The calendar spec names its time zone, so the timer follows the Eastern clock through daylight
saving on its own. ``uninstall`` is the rollback: it stops the timer and removes the two unit files.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

NODE_NAME = "node_daily_line_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")
_SAFE_VALUE = re.compile(r"^[^\"\\%$\n\r]*$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ModelDailyLineCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    on_calendar: str = Field(min_length=1)
    time_zone: str = Field(min_length=1)
    randomized_delay: str
    persistent: bool
    description: str


class ModelDailyLineSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    state_directory: str
    timeout_seconds: int = Field(ge=1)
    #: Variables copied into the unit from the installing environment when set (the bus routes the run reads).
    unit_environment: tuple[str, ...] = ()
    cadence: ModelDailyLineCadence

    @property
    def calendar(self) -> str:
        """The ``OnCalendar`` value: the wall-clock spec followed by its time zone."""
        return f"{self.cadence.on_calendar} {self.cadence.time_zone}"


def load_schedule(path: Path = CONTRACT_PATH) -> ModelDailyLineSchedule:
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelDailyLineSchedule.model_validate(contract["schedule"])


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


def _carried_environment(
    schedule: ModelDailyLineSchedule, environment: Mapping[str, str]
) -> str:
    lines = []
    for name in schedule.unit_environment:
        value = environment.get(name, "").strip()
        if not value:
            continue
        if not _SAFE_VALUE.match(value):
            raise ValueError(
                f"{name} holds a character a quoted unit line cannot carry"
            )
        lines.append(f'Environment="{name}={value}"\n')
    return "".join(lines)


def render_systemd(
    schedule: ModelDailyLineSchedule,
    *,
    omni_root: Path,
    home: Path,
    python: Path,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """The systemd user service and timer text."""
    root = _safe(omni_root, "registry root")
    user_home = _safe(home, "home")
    interpreter = _safe(python, "interpreter")
    state = _safe(omni_root / schedule.state_directory, "state directory")
    service = (
        "# Rendered by the daily line node (schedule install); the node contract owns the\n"
        "# cadence, so change the contract and install again rather than editing this file.\n"
        "# Rollback: schedule uninstall.\n"
        "[Unit]\nDescription=ONEX daily line (plan section 11)\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={schedule.timeout_seconds}\n"
        f'Environment="HOME={user_home}"\n'
        f'Environment="OMNI_HOME={root}"\n'
        f'Environment="PATH={user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin"\n'
        f"{_carried_environment(schedule, {} if environment is None else environment)}"
        f"WorkingDirectory={root}\n"
        f"ExecStart={interpreter} -m {NODE_MODULE} daily\n"
        f"StandardOutput=append:{state}/daily-line.out.log\n"
        f"StandardError=append:{state}/daily-line.err.log\n"
        f"SyslogIdentifier={schedule.unit_name}\n"
    )
    cadence = schedule.cadence
    timer = (
        "# Rendered by the daily line node (schedule install). Rollback: schedule uninstall.\n"
        f"[Unit]\nDescription={cadence.description}\n"
        "\n[Timer]\n"
        f"OnCalendar={schedule.calendar}\n"
        f"RandomizedDelaySec={cadence.randomized_delay}\n"
        f"Persistent={'true' if cadence.persistent else 'false'}\n"
        f"Unit={schedule.unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def unit_paths(schedule: ModelDailyLineSchedule, home: Path) -> tuple[Path, Path]:
    directory = home / ".config" / "systemd" / "user"
    return (
        directory / f"{schedule.unit_name}.service",
        directory / f"{schedule.unit_name}.timer",
    )


def install(
    schedule: ModelDailyLineSchedule,
    *,
    omni_root: Path,
    home: Path,
    python: Path,
    runner: Runner = run_command,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Write the two units, reload the user manager and enable the timer. Returns the first non-zero exit, else 0."""
    service_text, timer_text = render_systemd(
        schedule,
        omni_root=omni_root,
        home=home,
        python=python,
        environment=os.environ if environment is None else environment,
    )
    service_path, timer_path = unit_paths(schedule, home)
    service_path.parent.mkdir(parents=True, exist_ok=True)
    (omni_root / schedule.state_directory).mkdir(parents=True, exist_ok=True)
    service_path.write_text(service_text, encoding="utf-8")
    timer_path.write_text(timer_text, encoding="utf-8")
    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", f"{schedule.unit_name}.timer"],
    ):
        done = runner(command)
        if done.returncode != 0:
            return done.returncode
    return 0


def uninstall(
    schedule: ModelDailyLineSchedule, *, home: Path, runner: Runner = run_command
) -> int:
    """Stop and disable the timer, remove the two unit files and reload. The rollback of ``install``."""
    code = runner(
        ["systemctl", "--user", "disable", "--now", f"{schedule.unit_name}.timer"]
    ).returncode
    for path in unit_paths(schedule, home):
        path.unlink(missing_ok=True)
    reload_code = runner(["systemctl", "--user", "daemon-reload"]).returncode
    return code or reload_code
