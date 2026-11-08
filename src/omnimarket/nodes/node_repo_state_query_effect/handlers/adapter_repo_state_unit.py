# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render, install, remove and read the ``repo-state serve`` unit on a declared host (OMN-23033).

The unit declaration is the overlay's ``unit`` block (``ModelRepoStateUnit``): which hosts run the
serve process, the bus profile it serves on, the interpreter and the environment, all from the
deployed overlay. This module owns only how a systemd user service is written from that block.
``install`` writes the unit file, reloads the manager and enables the service now; ``uninstall`` is
the rollback: it disables and stops the service and removes the one file. A unit that would run
from a lane worktree is refused before anything is written.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.rules_unit_roots import refuse_worktree_roots
from omnimarket.nodes.node_repo_state_query_effect.models.model_repo_state_overlay import (
    ModelRepoStateOverlay,
)

NODE_MODULE = "omnimarket.nodes.node_repo_state_query_effect"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ModelRepoStateUnitResult(BaseModel):
    """What one installer verb did: the exit code and what it printed."""

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


def _fill(value: str, *, omni_root: Path, home: Path) -> str:
    return value.replace("$OMNI_HOME", str(omni_root)).replace("$HOME", str(home))


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


def render_unit(
    overlay: ModelRepoStateOverlay,
    host: str,
    *,
    omni_root: Path,
    home: Path,
    overlay_root: Path,
) -> str:
    """The systemd user service text for ``host``; ``ValueError`` when the host is not declared."""
    unit = overlay.unit
    if not any(row.name == host for row in unit.hosts):
        raise ValueError(
            f"{host}: not in the overlay's unit.hosts {sorted(row.name for row in unit.hosts)}, "
            "so the unit is not declared for it"
        )

    def fill(value: str) -> str:
        return _fill(value, omni_root=omni_root, home=home)

    python = fill(unit.python)
    log_file = fill(unit.log_file)
    if not _SAFE_PATH.match(python) or not _SAFE_PATH.match(log_file):
        raise ValueError(
            "the interpreter or the log file holds a character a unit line cannot carry unquoted"
        )
    environment = {
        "HOME": str(home),
        "OMNI_HOME": str(omni_root),
        "ONEX_SKILL_OVERLAY_ROOTS": str(overlay_root),
        **{name: fill(value) for name, value in unit.environment.items()},
    }
    if unit.pythonpath:
        environment["PYTHONPATH"] = ":".join(fill(path) for path in unit.pythonpath)
    return (
        "# Rendered by the repo-state node (repo-state unit install) from the deployed overlay;\n"
        "# the overlay owns every value, so change the overlay and install again rather than\n"
        "# editing this file. Rollback: repo-state unit uninstall.\n"
        f"[Unit]\nDescription={unit.description}\nStartLimitIntervalSec=0\n"
        "\n[Service]\nType=simple\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={str(omni_root).replace('%', '%%')}\n"
        f"ExecStart={_quote(python)} -m {NODE_MODULE} serve "
        f"--profile {_quote(unit.bus_profile)} --host-name {_quote(host)}\n"
        "Restart=on-failure\n"
        f"RestartSec={unit.restart_seconds}\n"
        f"TimeoutStopSec={unit.stop_timeout_seconds}\n"
        "KillSignal=SIGTERM\n"
        f"StandardOutput=append:{log_file}\n"
        "StandardError=inherit\n"
        f"SyslogIdentifier={unit.unit_name}\n"
        "\n[Install]\nWantedBy=default.target\n"
    )


class RepoStateUnitInstaller:
    """Check, install, remove and read the serve unit on one host."""

    def __init__(
        self,
        *,
        overlay: ModelRepoStateOverlay,
        home: Path,
        run: Runner = run_command,
    ) -> None:
        self.overlay = overlay
        self.home = home
        self.run = run
        self.service_name = f"{overlay.unit.unit_name}.service"
        self.unit_dir = home / ".config/systemd/user"

    def _declared(self, host: str) -> ModelRepoStateUnitResult | None:
        if any(row.name == host for row in self.overlay.unit.hosts):
            return None
        return ModelRepoStateUnitResult(
            exit_code=1,
            stderr=f"{host}: not in the overlay's unit.hosts, so the unit is not declared for it\n",
        )

    def render(
        self, host: str, *, omni_root: Path, overlay_root: Path
    ) -> ModelRepoStateUnitResult:
        try:
            text = render_unit(
                self.overlay,
                host,
                omni_root=omni_root,
                home=self.home,
                overlay_root=overlay_root,
            )
            refuse_worktree_roots({self.service_name: text})
        except ValueError as exc:
            return ModelRepoStateUnitResult(exit_code=1, stderr=f"{exc}\n")
        return ModelRepoStateUnitResult(exit_code=0, stdout=text)

    def install(
        self,
        host: str,
        *,
        omni_root: Path,
        overlay_root: Path,
        dry_run: bool = False,
    ) -> ModelRepoStateUnitResult:
        rendered = self.render(host, omni_root=omni_root, overlay_root=overlay_root)
        if rendered.exit_code or dry_run:
            return rendered
        unit = self.overlay.unit
        python = Path(_fill(unit.python, omni_root=omni_root, home=self.home))
        if not python.is_file():
            return ModelRepoStateUnitResult(
                exit_code=1, stderr=f"no interpreter at {python}\n"
            )
        environment = {
            "PYTHONPATH": ":".join(
                _fill(path, omni_root=omni_root, home=self.home)
                for path in unit.pythonpath
            )
        }
        probe = self.run(
            [
                "env",
                *(f"{name}={value}" for name, value in environment.items() if value),
                str(python),
                "-c",
                f"import {NODE_MODULE}.handlers.adapter_repo_state_query_serve",
            ]
        )
        if probe.returncode:
            return ModelRepoStateUnitResult(
                exit_code=1,
                stderr=(
                    f"{python} cannot import the serve module (exit {probe.returncode}); "
                    f"the unit would not start\n{probe.stderr}"
                ),
            )
        log_file = Path(_fill(unit.log_file, omni_root=omni_root, home=self.home))
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        target = self.unit_dir / self.service_name
        if target.exists() and target.read_text(encoding="utf-8") == rendered.stdout:
            output = f"{self.service_name} unchanged\n"
        else:
            target.write_text(rendered.stdout, encoding="utf-8")
            output = f"wrote {target}\n"
        for args in (["daemon-reload"], ["enable", "--now", self.service_name]):
            done = self.run(["systemctl", "--user", *args])
            output += done.stdout
            if done.returncode:
                return ModelRepoStateUnitResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        return ModelRepoStateUnitResult(
            exit_code=0, stdout=output + f"installed {self.service_name} on {host}\n"
        )

    def uninstall(self, host: str) -> ModelRepoStateUnitResult:
        """The rollback: stop and disable the service, remove the one unit file."""
        refused = self._declared(host)
        if refused is not None:
            return refused
        output = ""
        done = self.run(["systemctl", "--user", "disable", "--now", self.service_name])
        output += done.stdout
        target = self.unit_dir / self.service_name
        if target.exists():
            target.unlink()
            output += f"removed {target}\n"
        reloaded = self.run(["systemctl", "--user", "daemon-reload"])
        output += reloaded.stdout
        if reloaded.returncode:
            return ModelRepoStateUnitResult(
                exit_code=reloaded.returncode, stdout=output, stderr=reloaded.stderr
            )
        return ModelRepoStateUnitResult(
            exit_code=0, stdout=output + f"uninstalled {self.service_name}\n"
        )

    def status(self, host: str) -> ModelRepoStateUnitResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        active = self.run(["systemctl", "--user", "is-active", self.service_name])
        enabled = self.run(["systemctl", "--user", "is-enabled", self.service_name])
        return ModelRepoStateUnitResult(
            exit_code=0 if active.returncode == 0 else 1,
            stdout=(
                f"is-active: {active.stdout.strip() or active.stderr.strip()}\n"
                f"is-enabled: {enabled.stdout.strip() or enabled.stderr.strip()}\n"
            ),
        )


__all__ = [
    "ModelRepoStateUnitResult",
    "RepoStateUnitInstaller",
    "render_unit",
    "run_command",
]
