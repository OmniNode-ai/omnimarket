# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime parts the delegation readiness scenario nodes share (OMN-23171, OMN-23172).

* an event sink (in memory by default, the bus produce command as the real backend),
* the ledger appender over the bus append command,
* an atomic file write,
* the systemd user timer a scenario node installs for itself from its contract's ``schedule`` block.

Every setting arrives from the node's contract or from the environment variable the contract names.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

LedgerAppender = Callable[[str], None]


class ProtocolScenarioEventSink(Protocol):
    def emit(self, topic: str, payload: Mapping[str, object]) -> None: ...


class InMemoryScenarioEventSink:
    """The in-memory default: every event kept in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def emit(self, topic: str, payload: Mapping[str, object]) -> None:
        self.events.append((topic, dict(payload)))


class CommandScenarioEventSink:
    """The real backend: ``<produce command> <topic> --envelope --requested-by <lane>`` with the payload on stdin.

    A non-zero exit, a timeout or a missing command raises; a result that did not reach the bus is never silent.
    """

    def __init__(
        self, argv: Sequence[str], requested_by: str, timeout_seconds: int
    ) -> None:
        self._argv = list(argv)
        self._requested_by = requested_by
        self._timeout = timeout_seconds

    def emit(self, topic: str, payload: Mapping[str, object]) -> None:
        command = [
            *self._argv,
            topic,
            "--envelope",
            "--requested-by",
            self._requested_by,
        ]
        try:
            done = subprocess.run(
                command,
                input=json.dumps(payload, default=str),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                f"the bus emit of {topic} failed: {type(exc).__name__}"
            ) from exc
        if done.returncode != 0:
            raise RuntimeError(
                f"the bus emit of {topic} exited {done.returncode}: {done.stderr.strip()[:300]}"
            )


class JournalScenarioEventSink:
    """Appends each event as one JSON line (topic and payload); a reading or the floor reads the journal."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def emit(self, topic: str, payload: Mapping[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as sink:
            sink.write(
                json.dumps({"topic": topic, "payload": payload}, default=str) + "\n"
            )


class CompositeScenarioEventSink:
    """Every sink gets every event; the first failure is raised after all have been tried."""

    def __init__(self, *sinks: ProtocolScenarioEventSink) -> None:
        self._sinks = sinks

    def emit(self, topic: str, payload: Mapping[str, object]) -> None:
        failures: list[Exception] = []
        for sink in self._sinks:
            try:
                sink.emit(topic, payload)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise failures[0]


def build_event_sink(
    environment: Mapping[str, str],
    *,
    command_env: str,
    journal: Path,
    requested_by: str,
    timeout_seconds: int,
) -> ProtocolScenarioEventSink:
    """The journal always; the bus produce command too when the deployment names one in ``command_env``."""
    argv = shlex.split(environment.get(command_env, ""))
    journal_sink = JournalScenarioEventSink(journal)
    if not argv:
        return journal_sink
    return CompositeScenarioEventSink(
        journal_sink, CommandScenarioEventSink(argv, requested_by, timeout_seconds)
    )


def bus_appender(
    appender: ProtocolReadingAppender,
    *,
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> LedgerAppender:
    """Append a row over the bus. Anything but an accepted or duplicate receipt raises."""

    def append(row: str) -> None:
        outcome = appender.append(row, new_id())
        if not outcome.ok:
            raise RuntimeError(
                f"the ledger append was {outcome.status}: {outcome.message[:300]}"
            )

    return append


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")
_SAFE_VALUE = re.compile(r"^[^\"\\%$\n\r]*$")
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ModelScenarioCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    on_calendar: str = Field(min_length=1)
    time_zone: str = Field(min_length=1)
    randomized_delay: str
    persistent: bool
    description: str


class ModelScenarioSchedule(BaseModel):
    """The contract's ``schedule`` block: one systemd user timer that runs the node's ``run`` verb."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    state_directory: str
    timeout_seconds: int = Field(ge=1)
    #: Variables copied into the unit from the installing environment when set (the bus routes the run reads).
    unit_environment: tuple[str, ...] = ()
    #: A file under the home directory the unit loads when it exists (``-`` form), for values that must not sit in the unit.
    environment_file: str | None = None
    cadence: ModelScenarioCadence

    @property
    def calendar(self) -> str:
        return f"{self.cadence.on_calendar} {self.cadence.time_zone}"


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
    schedule: ModelScenarioSchedule, environment: Mapping[str, str]
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


def _environment_file_line(schedule: ModelScenarioSchedule, user_home: str) -> str:
    if schedule.environment_file is None:
        return ""
    path = _safe(Path(user_home) / schedule.environment_file, "environment file")
    return f"EnvironmentFile=-{path}\n"


def render_systemd(
    schedule: ModelScenarioSchedule,
    *,
    node_module: str,
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
        "# Rendered by the node (schedule install); the node contract owns the cadence,\n"
        "# so change the contract and install again rather than editing this file.\n"
        "# Rollback: schedule uninstall.\n"
        f"[Unit]\nDescription={schedule.cadence.description}\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={schedule.timeout_seconds}\n"
        f'Environment="HOME={user_home}"\n'
        f'Environment="OMNI_HOME={root}"\n'
        f'Environment="PATH={user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin"\n'
        f"{_carried_environment(schedule, {} if environment is None else environment)}"
        f"{_environment_file_line(schedule, user_home)}"
        f"WorkingDirectory={root}\n"
        f"ExecStart={interpreter} -m {node_module} run\n"
        f"StandardOutput=append:{state}/{schedule.unit_name}.out.log\n"
        f"StandardError=append:{state}/{schedule.unit_name}.err.log\n"
        f"SyslogIdentifier={schedule.unit_name}\n"
    )
    cadence = schedule.cadence
    timer = (
        "# Rendered by the node (schedule install). Rollback: schedule uninstall.\n"
        f"[Unit]\nDescription={cadence.description}\n"
        "\n[Timer]\n"
        f"OnCalendar={schedule.calendar}\n"
        f"RandomizedDelaySec={cadence.randomized_delay}\n"
        f"Persistent={'true' if cadence.persistent else 'false'}\n"
        f"Unit={schedule.unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def unit_paths(schedule: ModelScenarioSchedule, home: Path) -> tuple[Path, Path]:
    directory = home / ".config" / "systemd" / "user"
    return (
        directory / f"{schedule.unit_name}.service",
        directory / f"{schedule.unit_name}.timer",
    )


def install_schedule(
    schedule: ModelScenarioSchedule,
    *,
    node_module: str,
    omni_root: Path,
    home: Path,
    python: Path,
    runner: Runner = run_command,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Write the two units, reload the user manager and enable the timer. Returns the first non-zero exit, else 0."""
    service_text, timer_text = render_systemd(
        schedule,
        node_module=node_module,
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


def uninstall_schedule(
    schedule: ModelScenarioSchedule, *, home: Path, runner: Runner = run_command
) -> int:
    """Stop and disable the timer, remove the two unit files and reload. The rollback of ``install_schedule``."""
    code = runner(
        ["systemctl", "--user", "disable", "--now", f"{schedule.unit_name}.timer"]
    ).returncode
    for path in unit_paths(schedule, home):
        path.unlink(missing_ok=True)
    reload_code = runner(["systemctl", "--user", "daemon-reload"]).returncode
    return code or reload_code
