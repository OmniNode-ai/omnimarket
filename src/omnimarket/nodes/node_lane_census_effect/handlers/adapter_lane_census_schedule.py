# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane census node's own schedule: render, check and install it on a Docker Engine host.

The cadence is the contract's ``schedule`` block and the hosts are the node overlay. A host row
that names no Docker Engine, or a scheduler other than a systemd user timer, does not validate,
and ``check`` and ``install`` refuse a duplicate row by naming the host: the census belongs on a
host whose Docker is always running, never on a Docker Desktop host a person must have started.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_lane_census import (
    ModelLaneCensusHostRow,
    ModelLaneCensusSchedule,
    ModelLaneCensusScheduleOverlay,
    ModelLaneCensusScheduleResult,
)
from omnimarket.models.overlay_keys import ModelLabHostsTable

NODE_NAME = "node_lane_census_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

# A path is written unquoted into unit lines such as ``StandardOutput=append:``.
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a host command; a missing program reads as exit status 127."""
    try:
        return subprocess.run(
            list(command), capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(command), 127, "", str(exc))


def load_schedule(path: Path = CONTRACT_PATH) -> ModelLaneCensusSchedule:
    """The ``schedule`` block of the node contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelLaneCensusSchedule.model_validate(contract["schedule"])


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelLaneCensusScheduleOverlay:
    """The node overlay: ``<root>/node_lane_census_effect/overlay.yaml``."""
    return load_structured_overlay(NODE_NAME, ModelLaneCensusScheduleOverlay, environ)


def schedule_gaps(
    overlay: ModelLaneCensusScheduleOverlay,
    lab_table: ModelLabHostsTable | None = None,
) -> tuple[str, ...]:
    """Every way the declared hosts cannot run the census; each line names its host.

    With ``lab_table``, a declared host the table does not have, an unavailable one, and one whose
    row says ``docker: false`` (every macOS row) are gaps too: the host table, not the overlay
    row's own word, is what says the host takes Docker work.
    """
    gaps: list[str] = []
    seen: set[str] = set()
    for row in overlay.hosts:
        if row.name in seen:
            gaps.append(f"{row.name}: declared more than once")
        seen.add(row.name)
    if lab_table is not None:
        rows = {host.name: host for host in lab_table.hosts}
        for name in sorted(seen):
            host = rows.get(name)
            if host is None:
                gaps.append(f"{name}: names a host the lab host table does not have")
            elif not host.available:
                gaps.append(f"{name}: the lab host table marks the host unavailable")
            elif host.docker is False:
                gaps.append(
                    f"{name}: the lab host table says docker: false, so the host takes no Docker work"
                )
    return tuple(gaps)


def _safe(path: Path, what: str) -> str:
    text = str(path)
    if not _SAFE_PATH.match(text):
        raise ValueError(
            f"{what} {text!r} holds a character a unit line cannot carry unquoted"
        )
    return text


def _quote(value: str) -> str:
    """One systemd command-line word: quoted, with the backslash, quote, ``%`` and ``$`` escaped."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def _assignment(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{name}={escaped}"\n'


def render_units(
    schedule: ModelLaneCensusSchedule,
    *,
    omni_root: Path,
    venv: Path,
    home: Path,
    config_root: Path | None = None,
) -> tuple[str, str]:
    """The systemd user service and timer text of the scheduled census."""
    root = _safe(omni_root, "registry root")
    python = _safe(venv / "bin" / "python", "dispatch venv interpreter")
    user_home = _safe(home, "home")
    state = _safe(omni_root / schedule.state_directory, "state directory")
    cadence = schedule.cadence
    environment = {
        "HOME": user_home,
        "OMNI_HOME": root,
        "ONEX_DISPATCH_VENV": _safe(venv, "dispatch venv"),
        "LANE_CENSUS_PYTHON": python,
        "PATH": f"{user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
    }
    if config_root is not None:
        environment["ONEX_WORKSPACE_CONFIG_ROOT"] = _safe(config_root, "config root")
    service = (
        "# Rendered by the lane census node (schedule install); the node contract owns the\n"
        "# cadence, so change the contract and install again rather than editing this file.\n"
        "[Unit]\nDescription=ONEX lane census (Docker inventory against the lane manifest)\n"
        "After=docker.service\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={schedule.timeout_seconds}\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={root.replace('%', '%%')}\n"
        f"ExecStart={_quote(python)} -m {NODE_MODULE} --omni-home {_quote(root)} "
        f"--snapshot {_quote(state + '/' + schedule.snapshot_name)}\n"
        f"StandardOutput=append:{state}/lane-census.out.log\n"
        f"StandardError=append:{state}/lane-census.err.log\n"
        f"SyslogIdentifier={schedule.unit_name}\n"
    )
    timer = (
        f"# {cadence.description}\n"
        "[Unit]\nDescription=ONEX lane census tick\n"
        f"\n[Timer]\nOnCalendar={cadence.on_calendar}\n"
        f"RandomizedDelaySec={cadence.randomized_delay}\n"
        f"Persistent={'true' if cadence.persistent else 'false'}\n"
        f"Unit={schedule.unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


class LaneCensusScheduler:
    """Check and install the census timer on one host."""

    def __init__(
        self,
        *,
        schedule: ModelLaneCensusSchedule,
        overlay: ModelLaneCensusScheduleOverlay,
        home: Path,
        dispatch_venv: Path | None = None,
        run: Runner = run_command,
    ) -> None:
        self.schedule = schedule
        self.overlay = overlay
        self.home = home
        self.dispatch_venv = dispatch_venv
        self.run = run
        self.service_name = f"{schedule.unit_name}.service"
        self.timer_name = f"{schedule.unit_name}.timer"
        self.unit_dir = home / ".config/systemd/user"

    def check(
        self, lab_table: ModelLabHostsTable | None = None
    ) -> ModelLaneCensusScheduleResult:
        gaps = schedule_gaps(self.overlay, lab_table)
        if gaps:
            return ModelLaneCensusScheduleResult(
                exit_code=1, stderr="".join(f"GAP {gap}\n" for gap in gaps)
            )
        return ModelLaneCensusScheduleResult(
            exit_code=0,
            stdout=f"OK {len(self.overlay.hosts)} hosts, each a Docker Engine host\n",
        )

    def install(
        self,
        host: str,
        *,
        omni_root: Path,
        config_root: Path | None = None,
        dry_run: bool = False,
    ) -> ModelLaneCensusScheduleResult:
        refused = self._resolve(host)
        if isinstance(refused, ModelLaneCensusScheduleResult):
            return refused
        venv = self.dispatch_venv or omni_root / self.schedule.dispatch_venv
        python = venv / "bin" / "python"
        try:
            service, timer = render_units(
                self.schedule,
                omni_root=omni_root,
                venv=venv,
                home=self.home,
                config_root=config_root,
            )
        except ValueError as exc:
            return ModelLaneCensusScheduleResult(exit_code=1, stderr=f"{exc}\n")
        if dry_run:
            return ModelLaneCensusScheduleResult(
                exit_code=0, stdout=f"{service}\n{timer}"
            )
        if not python.is_file():
            return ModelLaneCensusScheduleResult(
                exit_code=1, stderr=f"no dispatch venv interpreter at {python}\n"
            )
        started = self.run([str(python), "-c", f"import {NODE_MODULE}.__main__"])
        if started.returncode:
            return ModelLaneCensusScheduleResult(
                exit_code=1,
                stderr=(
                    f"{python} cannot import {NODE_MODULE}.__main__ "
                    f"(exit {started.returncode}); the census would not start\n"
                    f"{started.stderr}"
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
                return ModelLaneCensusScheduleResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        return ModelLaneCensusScheduleResult(
            exit_code=0,
            stdout=output
            + f"installed {self.timer_name}: {self.schedule.cadence.description}\n",
        )

    def run_now(self, host: str) -> ModelLaneCensusScheduleResult:
        refused = self._resolve(host)
        if isinstance(refused, ModelLaneCensusScheduleResult):
            return refused
        started = self.run(["systemctl", "--user", "start", self.service_name])
        return ModelLaneCensusScheduleResult(
            exit_code=started.returncode,
            stdout=started.stdout or f"ran {self.service_name} once\n",
            stderr=started.stderr,
        )

    def _resolve(
        self, host: str
    ) -> ModelLaneCensusHostRow | ModelLaneCensusScheduleResult:
        """The host's row, or the refusal: any gap in the overlay, or no row for the host."""
        gaps = schedule_gaps(self.overlay)
        if gaps:
            return ModelLaneCensusScheduleResult(
                exit_code=1, stderr="".join(f"GAP {gap}\n" for gap in gaps)
            )
        for row in self.overlay.hosts:
            if row.name == host:
                return row
        return ModelLaneCensusScheduleResult(
            exit_code=1,
            stderr=f"{host}: not in the {NODE_NAME} overlay, so the census is not declared for it\n",
        )
