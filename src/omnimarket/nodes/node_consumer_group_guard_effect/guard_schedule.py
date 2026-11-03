# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render, install and report the guard's contract-owned systemd user timer."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

from .guard_schedule_models import (
    ModelGuardHostRow,
    ModelGuardSchedule,
    ModelGuardScheduleOverlay,
    ModelGuardScheduleResult,
)

NODE_NAME = "node_consumer_group_guard_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a host command; a missing program reads as exit status 127."""
    try:
        return subprocess.run(
            list(command), capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(command), 127, "", str(exc))


def load_schedule(path: Path = CONTRACT_PATH) -> ModelGuardSchedule:
    """Read only the schedule block of the node contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(contract, dict) or "schedule" not in contract:
        raise ValueError(f"{path}: missing schedule block")
    return ModelGuardSchedule.model_validate(contract["schedule"])


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelGuardScheduleOverlay:
    """Read the structured node overlay from the configured overlay roots."""
    return load_structured_overlay(NODE_NAME, ModelGuardScheduleOverlay, environ)


def schedule_gaps(overlay: ModelGuardScheduleOverlay) -> tuple[str, ...]:
    """Name every host declared more than once; required fields validate on load."""
    seen: set[str] = set()
    gaps: list[str] = []
    for row in overlay.hosts:
        if row.name in seen:
            gaps.append(f"{row.name}: declared more than once")
        seen.add(row.name)
    return tuple(gaps)


def _safe(path: Path, what: str) -> str:
    text = str(path)
    if not _SAFE_PATH.match(text):
        raise ValueError(
            f"{what} {text!r} holds a character a unit line cannot carry unquoted"
        )
    return text


def _quote(value: str) -> str:
    """Quote one systemd command-line word, escaping specifiers and variables."""
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
    schedule: ModelGuardSchedule,
    row: ModelGuardHostRow,
    *,
    omni_root: Path,
    venv: Path,
    home: Path,
) -> tuple[str, str]:
    """The systemd user service and timer text of the scheduled guard on one host."""
    state_dir = omni_root / schedule.state_directory
    root = _safe(omni_root, "workspace root")
    python = _safe(venv / "bin/python", "dispatch venv interpreter")
    state = _safe(state_dir, "state directory")
    user_home = _safe(home, "home")
    cadence = schedule.cadence
    environment = {
        "HOME": user_home,
        "PATH": f"{user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "ONEX_CONSUMER_GROUP_GUARD_BUS_LANE": row.bus_lane,
        "ONEX_WORKSPACE_CONFIG_ROOT": row.workspace_config_root,
    }
    service = (
        "# Rendered by the consumer-group guard node (schedule install).\n"
        "[Unit]\nDescription=ONEX consumer-group guard (census and reap of long-Empty groups)\n"
        "\n[Service]\nType=oneshot\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={root}\n"
        f"ExecStart={_quote(python)} -m {NODE_MODULE} --state-dir {_quote(state)}\n"
        f"StandardOutput=append:{state}/consumer-group-guard.out.log\n"
        f"StandardError=append:{state}/consumer-group-guard.err.log\n"
        f"SyslogIdentifier={schedule.unit_name}\n"
    )
    timer = (
        f"# {cadence.description}\n"
        "[Unit]\nDescription=ONEX consumer-group guard tick\n"
        f"\n[Timer]\nOnActiveSec={cadence.first_pass}\n"
        f"OnUnitActiveSec={cadence.interval_seconds}s\n"
        f"AccuracySec={cadence.accuracy}\nRandomizedDelaySec=0\n"
        f"Unit={schedule.unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def judge_census(
    census_text: str | None, now: datetime, interval_seconds: int
) -> tuple[bool, str]:
    """Require a recent OK census and a completed reap, allowing 60s clock skew."""
    if census_text is None:
        return False, "census.json is absent"
    try:
        census = json.loads(census_text)
    except ValueError:
        return False, "census.json is unreadable JSON"
    if not isinstance(census, dict):
        return False, "census.json must be an object"
    observed_at = census.get("observed_at")
    if not isinstance(observed_at, str):
        return False, "observed_at is missing or unparseable"
    try:
        observed = datetime.fromisoformat(observed_at)
        if observed.tzinfo is None:
            return False, "observed_at has no timezone"
        age = (now - observed).total_seconds()
    except ValueError:
        return False, "observed_at is unparseable"
    if age > 2 * interval_seconds:
        return False, f"census.json is stale ({age:.0f}s old)"
    if age < -60:
        return False, "observed_at is too far in the future"
    if census.get("status") != "OK":
        return False, f"census status is {census.get('status')!r}"
    detail = census.get("reap_detail")
    if isinstance(detail, str):
        counts = re.match(r"^reaped (\d+) of (\d+) eligible", detail)
        if detail.startswith("nothing to reap among") or (
            counts and int(counts[1]) == int(counts[2])
        ):
            return True, f"census.json observed_at={observed_at}; {detail}"
    return False, f"reap not completed: {detail!r}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class GuardScheduler:
    """Install, check and report the guard's timer on one declared host."""

    def __init__(
        self,
        *,
        schedule: ModelGuardSchedule,
        overlay: ModelGuardScheduleOverlay,
        home: Path,
        dispatch_venv: Path | None = None,
        run: Runner = run_command,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.schedule = schedule
        self.overlay = overlay
        self.home = home
        self.dispatch_venv = dispatch_venv
        self.run = run
        self.now = now
        self.service_name = f"{schedule.unit_name}.service"
        self.timer_name = f"{schedule.unit_name}.timer"
        self.unit_dir = home / ".config/systemd/user"

    def check(self) -> ModelGuardScheduleResult:
        gaps = schedule_gaps(self.overlay)
        if gaps:
            return ModelGuardScheduleResult(
                exit_code=1, stderr="".join(f"GAP {gap}\n" for gap in gaps)
            )
        return ModelGuardScheduleResult(
            exit_code=0,
            stdout=f"OK {len(self.overlay.hosts)} hosts, each with a scheduler\n",
        )

    def install(
        self, host: str, *, omni_root: Path, dry_run: bool = False
    ) -> ModelGuardScheduleResult:
        row = self._resolve(host)
        if isinstance(row, ModelGuardScheduleResult):
            return row
        venv = self.dispatch_venv or omni_root / self.schedule.dispatch_venv
        python = venv / "bin/python"
        state_dir = omni_root / self.schedule.state_directory
        try:
            service, timer = render_units(
                self.schedule, row, omni_root=omni_root, venv=venv, home=self.home
            )
        except ValueError as exc:
            return ModelGuardScheduleResult(exit_code=1, stderr=f"{exc}\n")
        if dry_run:
            return ModelGuardScheduleResult(exit_code=0, stdout=f"{service}\n{timer}")
        if not python.is_file():
            return ModelGuardScheduleResult(
                exit_code=1, stderr=f"no dispatch venv interpreter at {python}\n"
            )
        started = self.run([str(python), "-c", f"import {NODE_MODULE}.__main__"])
        if started.returncode:
            return ModelGuardScheduleResult(
                exit_code=1,
                stderr=(
                    f"{python} cannot import {NODE_MODULE}.__main__ "
                    f"(exit {started.returncode}); the guard would not start\n"
                    f"{started.stderr}"
                ),
            )
        output = ""
        state_dir.mkdir(parents=True, exist_ok=True)
        self.unit_dir.mkdir(parents=True, exist_ok=True)
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
                return ModelGuardScheduleResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        listed = self.run(
            ["systemctl", "--user", "list-timers", self.timer_name, "--no-pager"]
        )
        output += listed.stdout
        if listed.returncode or self.timer_name not in listed.stdout:
            return ModelGuardScheduleResult(
                exit_code=1,
                stdout=output,
                stderr=f"{self.timer_name} is enabled but systemd lists no schedule for it\n",
            )
        output += (
            f"installed {self.timer_name}: {self.schedule.cadence.description}, "
            f"logs in {state_dir}\n"
        )
        return ModelGuardScheduleResult(exit_code=0, stdout=output)

    def status(self, host: str, *, omni_root: Path) -> ModelGuardScheduleResult:
        row = self._resolve(host)
        if isinstance(row, ModelGuardScheduleResult):
            return row
        active = self.run(["systemctl", "--user", "is-active", self.timer_name])
        loaded = active.returncode == 0
        output = f"{'LOADED' if loaded else 'NOT LOADED'} {self.timer_name}\n"
        if loaded:
            output += self.run(
                ["systemctl", "--user", "list-timers", self.timer_name, "--no-pager"]
            ).stdout
        census_path = omni_root / self.schedule.state_directory / "census.json"
        try:
            census_text = census_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            healthy, reason = judge_census(
                None, self.now(), self.schedule.cadence.interval_seconds
            )
        except (OSError, UnicodeError) as exc:
            healthy, reason = False, f"census.json is unreadable ({type(exc).__name__})"
        else:
            healthy, reason = judge_census(
                census_text, self.now(), self.schedule.cadence.interval_seconds
            )
        output += f"HEALTHY {reason}\n" if healthy else f"NOT HEALTHY: {reason}\n"
        return ModelGuardScheduleResult(
            exit_code=0 if loaded and healthy else 1, stdout=output
        )

    def run_now(self, host: str) -> ModelGuardScheduleResult:
        row = self._resolve(host)
        if isinstance(row, ModelGuardScheduleResult):
            return row
        started = self.run(["systemctl", "--user", "start", self.service_name])
        if started.returncode:
            return ModelGuardScheduleResult(
                exit_code=started.returncode,
                stdout=started.stdout,
                stderr=started.stderr,
            )
        return ModelGuardScheduleResult(
            exit_code=0, stdout=f"ran {self.service_name} once\n"
        )

    def uninstall(self, host: str) -> ModelGuardScheduleResult:
        row = self._resolve(host)
        if isinstance(row, ModelGuardScheduleResult):
            return row
        disabled = self.run(
            ["systemctl", "--user", "disable", "--now", self.timer_name]
        )
        (self.unit_dir / self.service_name).unlink(missing_ok=True)
        (self.unit_dir / self.timer_name).unlink(missing_ok=True)
        reloaded = self.run(["systemctl", "--user", "daemon-reload"])
        failed = disabled.returncode or reloaded.returncode
        return ModelGuardScheduleResult(
            exit_code=failed,
            stdout=(
                f"removed {self.timer_name} and {self.service_name} "
                "(the logs are left in place)\n"
            ),
            stderr=disabled.stderr + reloaded.stderr,
        )

    def _resolve(self, host: str) -> ModelGuardHostRow | ModelGuardScheduleResult:
        gaps = schedule_gaps(self.overlay)
        if gaps:
            return ModelGuardScheduleResult(
                exit_code=1, stderr="".join(f"GAP {gap}\n" for gap in gaps)
            )
        for row in self.overlay.hosts:
            if row.name == host:
                return row
        return ModelGuardScheduleResult(
            exit_code=1,
            stderr=f"{host}: not in the {NODE_NAME} overlay, so no scheduler is declared for it\n",
        )
