# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host reconcile node's own schedule: render, install, check and report it.

A host that was reconciled once by hand and never again keeps whatever the last run left in its
dispatch venv. The reconciler therefore runs on a timer on every host that runs it, and the node
that does the work owns that timer: the cadence is the contract's ``schedule`` block, the hosts
and what schedules the run on each are the node overlay, and a host that declares the reconciler
with no scheduler is a gap that ``check`` and ``install`` refuse by naming the host.

The timer starts the node's own entry point (``python -m`` of the dispatch venv's interpreter),
in repair mode, with the registry root and the tracked branch. Every path comes from the request
and the contract; nothing here names a host.
"""

from __future__ import annotations

import getpass
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_host_reconcile_schedule import (
    ModelHostReconcileHostRow,
    ModelHostReconcileSchedule,
    ModelHostReconcileScheduleOverlay,
    ModelHostReconcileScheduleResult,
)
from omnimarket.models.overlay_keys import ModelLabHostsTable

NODE_NAME = "node_host_reconcile_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
INSTALLED_SCHEDULER = "systemd-user"

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


def load_schedule(path: Path = CONTRACT_PATH) -> ModelHostReconcileSchedule:
    """The ``schedule`` block of the node contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelHostReconcileSchedule.model_validate(contract["schedule"])


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelHostReconcileScheduleOverlay:
    """The node overlay: ``<root>/node_host_reconcile_effect/overlay.yaml``.

    Raises ``NodeOverlayError`` naming how to supply it when no root holds one.
    """
    return load_structured_overlay(
        NODE_NAME, ModelHostReconcileScheduleOverlay, environ
    )


def schedule_gaps(
    schedule: ModelHostReconcileSchedule,
    overlay: ModelHostReconcileScheduleOverlay,
    lab_table: ModelLabHostsTable | None = None,
) -> tuple[str, ...]:
    """Every way the declared hosts fail to have a scheduler; each line names its host.

    With ``lab_table``, an available host of the lab host table that declares no reconciler is a
    gap too, and so is a declared host the table does not have.
    """
    kinds = sorted(schedule.schedulers)
    gaps: list[str] = []
    seen: set[str] = set()
    for row in overlay.hosts:
        if row.name in seen:
            gaps.append(f"{row.name}: declared more than once")
        seen.add(row.name)
        if row.scheduler is None:
            gaps.append(
                f"{row.name}: declares the reconciler with no scheduler; "
                f"set scheduler to one of {kinds}"
            )
        elif row.scheduler not in schedule.schedulers:
            gaps.append(
                f"{row.name}: scheduler {row.scheduler} is not one the contract "
                f"declares ({kinds})"
            )
        elif row.scheduler == INSTALLED_SCHEDULER:
            if row.scheduled_by is not None:
                gaps.append(
                    f"{row.name}: scheduled_by names another owner's scheduler; "
                    f"{INSTALLED_SCHEDULER} is installed by this node"
                )
        elif row.scheduled_by is None:
            gaps.append(
                f"{row.name}: scheduler {row.scheduler} must name scheduled_by, "
                "the unit or label that runs the reconciler"
            )
    if lab_table is not None:
        known = {host.name for host in lab_table.hosts}
        required = {host.name for host in lab_table.hosts if host.available}
        gaps.extend(
            f"{name}: runs lab work but declares no reconciler"
            for name in sorted(required - seen)
        )
        gaps.extend(
            f"{name}: names a host the lab host table does not have"
            for name in sorted(seen - known)
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
    schedule: ModelHostReconcileSchedule,
    *,
    omni_root: Path,
    venv: Path,
    home: Path,
) -> tuple[str, str]:
    """The systemd user service and timer text of the scheduled reconciler."""
    root = _safe(omni_root, "registry root")
    python = _safe(venv / "bin" / "python", "dispatch venv interpreter")
    user_home = _safe(home, "home")
    state = _safe(omni_root / schedule.state_directory, "state directory")
    cadence = schedule.cadence
    environment = {
        "HOME": user_home,
        "OMNI_HOME": root,
        "ONEX_DISPATCH_VENV": _safe(venv, "dispatch venv"),
        "PATH": f"{user_home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
    }
    service = (
        "# Rendered by the host reconcile node (schedule install); the node contract owns the\n"
        "# cadence, so change the contract and install again rather than editing this file.\n"
        "[Unit]\n"
        "Description=ONEX scheduled host reconciler (dispatch venv and clones to the branch)\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={schedule.timeout_seconds}\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={root.replace('%', '%%')}\n"
        f"ExecStart={_quote(python)} -m {NODE_MODULE} "
        f"--omni-home {_quote(root)} --branch {_quote(schedule.branch)}\n"
        f"StandardOutput=append:{state}/host-reconcile.out.log\n"
        f"StandardError=append:{state}/host-reconcile.err.log\n"
        f"SyslogIdentifier={schedule.unit_name}\n"
    )
    timer = (
        f"# {cadence.description}\n"
        "[Unit]\nDescription=ONEX scheduled host reconciler tick\n"
        f"\n[Timer]\nOnCalendar={cadence.on_calendar}\n"
        f"RandomizedDelaySec={cadence.randomized_delay}\n"
        f"Persistent={'true' if cadence.persistent else 'false'}\n"
        f"Unit={schedule.unit_name}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


class HostReconcileScheduler:
    """Install, check and report the reconciler's timer on one host."""

    def __init__(
        self,
        *,
        schedule: ModelHostReconcileSchedule,
        overlay: ModelHostReconcileScheduleOverlay,
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
    ) -> ModelHostReconcileScheduleResult:
        gaps = schedule_gaps(self.schedule, self.overlay, lab_table)
        if gaps:
            return ModelHostReconcileScheduleResult(
                exit_code=1, stderr="".join(f"GAP {gap}\n" for gap in gaps)
            )
        return ModelHostReconcileScheduleResult(
            exit_code=0,
            stdout=f"OK {len(self.overlay.hosts)} hosts, each with a scheduler\n",
        )

    def install(
        self, host: str, *, omni_root: Path, dry_run: bool = False
    ) -> ModelHostReconcileScheduleResult:
        refused = self._resolve(host)
        if isinstance(refused, ModelHostReconcileScheduleResult):
            return refused
        row = refused
        if row.scheduler != INSTALLED_SCHEDULER:
            return ModelHostReconcileScheduleResult(
                exit_code=0,
                stdout=(
                    f"{host}: scheduled by {row.scheduler} {row.scheduled_by}; "
                    f"the node installs only {INSTALLED_SCHEDULER}, nothing to do\n"
                ),
            )
        venv = self._venv(omni_root)
        python = venv / "bin" / "python"
        try:
            service, timer = render_units(
                self.schedule, omni_root=omni_root, venv=venv, home=self.home
            )
        except ValueError as exc:
            return ModelHostReconcileScheduleResult(exit_code=1, stderr=f"{exc}\n")
        if not python.is_file():
            return ModelHostReconcileScheduleResult(
                exit_code=1, stderr=f"no dispatch venv interpreter at {python}\n"
            )
        started = self.run([str(python), "-c", f"import {NODE_MODULE}.__main__"])
        if started.returncode:
            return ModelHostReconcileScheduleResult(
                exit_code=1,
                stderr=(
                    f"{python} cannot import {NODE_MODULE}.__main__ "
                    f"(exit {started.returncode}); the reconciler would not start\n"
                    f"{started.stderr}"
                ),
            )
        if dry_run:
            return ModelHostReconcileScheduleResult(
                exit_code=0, stdout=f"{service}\n{timer}"
            )
        output = ""
        (omni_root / self.schedule.state_directory).mkdir(parents=True, exist_ok=True)
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
                return ModelHostReconcileScheduleResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        listed = self.run(
            ["systemctl", "--user", "list-timers", self.timer_name, "--no-pager"]
        )
        output += listed.stdout
        if listed.returncode or self.timer_name not in listed.stdout:
            return ModelHostReconcileScheduleResult(
                exit_code=1,
                stdout=output,
                stderr=f"{self.timer_name} is enabled but systemd lists no schedule for it\n",
            )
        output += (
            f"installed {self.timer_name}: {self.schedule.cadence.description}, runs "
            f"{python} -m {NODE_MODULE} --omni-home {omni_root} --branch "
            f"{self.schedule.branch} (repair mode), logs in "
            f"{omni_root / self.schedule.state_directory}\n"
        )
        return ModelHostReconcileScheduleResult(exit_code=0, stdout=output)

    def status(self, host: str) -> ModelHostReconcileScheduleResult:
        row = self._resolve(host)
        if isinstance(row, ModelHostReconcileScheduleResult):
            return row
        if row.scheduler != INSTALLED_SCHEDULER:
            return ModelHostReconcileScheduleResult(
                exit_code=0,
                stdout=f"{host}: scheduled by {row.scheduler} {row.scheduled_by}\n",
            )
        output, failed = "", 0
        active = self.run(["systemctl", "--user", "is-active", self.timer_name])
        if active.returncode == 0:
            output += f"LOADED {self.timer_name}\n"
            output += self.run(
                ["systemctl", "--user", "list-timers", self.timer_name, "--no-pager"]
            ).stdout
        else:
            output += f"NOT LOADED {self.timer_name}\n"
            failed = 1
        linger = self.run(
            ["loginctl", "show-user", getpass.getuser(), "--property=Linger"]
        )
        if linger.stdout.strip() == "Linger=yes":
            output += "LINGER yes\n"
        else:
            output += (
                "LINGER OFF: a user timer does not fire while the user is logged out; "
                "run loginctl enable-linger for this user\n"
            )
            failed = 1
        last = self.run(
            [
                "systemctl",
                "--user",
                "show",
                self.service_name,
                "--property=Result,ExecMainStatus,ExecMainExitTimestamp",
            ]
        )
        output += last.stdout
        if "Result=success" not in last.stdout:
            failed = 1
        return ModelHostReconcileScheduleResult(exit_code=failed, stdout=output)

    def run_now(self, host: str) -> ModelHostReconcileScheduleResult:
        row = self._resolve(host)
        if isinstance(row, ModelHostReconcileScheduleResult):
            return row
        if row.scheduler != INSTALLED_SCHEDULER:
            return ModelHostReconcileScheduleResult(
                exit_code=2,
                stderr=f"{host}: scheduled by {row.scheduler}, not by this node\n",
            )
        started = self.run(["systemctl", "--user", "start", self.service_name])
        if started.returncode:
            return ModelHostReconcileScheduleResult(
                exit_code=started.returncode,
                stdout=started.stdout,
                stderr=started.stderr,
            )
        return ModelHostReconcileScheduleResult(
            exit_code=0, stdout=f"ran {self.service_name} once\n"
        )

    def uninstall(self, host: str) -> ModelHostReconcileScheduleResult:
        row = self._resolve(host)
        if isinstance(row, ModelHostReconcileScheduleResult):
            return row
        if row.scheduler != INSTALLED_SCHEDULER:
            return ModelHostReconcileScheduleResult(
                exit_code=2,
                stderr=f"{host}: scheduled by {row.scheduler}, not by this node\n",
            )
        self.run(["systemctl", "--user", "disable", "--now", self.timer_name])
        (self.unit_dir / self.service_name).unlink(missing_ok=True)
        (self.unit_dir / self.timer_name).unlink(missing_ok=True)
        self.run(["systemctl", "--user", "daemon-reload"])
        return ModelHostReconcileScheduleResult(
            exit_code=0,
            stdout=(
                f"removed {self.timer_name} and {self.service_name} "
                "(the logs are left in place)\n"
            ),
        )

    def _venv(self, omni_root: Path) -> Path:
        return self.dispatch_venv or omni_root / self.schedule.dispatch_venv

    def _resolve(
        self, host: str
    ) -> ModelHostReconcileHostRow | ModelHostReconcileScheduleResult:
        """The host's row, or the refusal: any gap in the overlay, or no row for the host."""
        gaps = schedule_gaps(self.schedule, self.overlay)
        if gaps:
            return ModelHostReconcileScheduleResult(
                exit_code=1, stderr="".join(f"GAP {gap}\n" for gap in gaps)
            )
        for row in self.overlay.hosts:
            if row.name == host:
                return row
        return ModelHostReconcileScheduleResult(
            exit_code=1,
            stderr=f"{host}: not in the {NODE_NAME} overlay, so no scheduler is declared for it\n",
        )
