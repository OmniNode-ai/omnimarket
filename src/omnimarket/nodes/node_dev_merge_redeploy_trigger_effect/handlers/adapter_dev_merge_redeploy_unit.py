# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render, install, remove and read the ``dev-merge-redeploy serve`` unit on a declared host (OMN-22478 R2).

The unit declaration is the overlay's ``unit`` block (``ModelDevMergeUnit``): which hosts run
the serve process, the bus it serves on, the interpreter and the environment, all from the
deployed overlay. This module owns only how a systemd user service is written from that block.
``install`` writes the unit file, reloads the manager and enables the service now; ``uninstall``
is the rollback: it disables and stops the service and removes the one file. A unit that would
run from a lane worktree is refused before anything is written, and so is one whose lane probe
cannot reach the lane: it would only write deadline_missed receipts.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.rules_unit_roots import refuse_worktree_roots
from omnimarket.models.dev_merge_redeploy_overlay import ModelDevMergeRedeployPolicy

NODE_MODULE = "omnimarket.nodes.node_dev_merge_redeploy_trigger_effect"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class ModelDevMergeUnitResult(BaseModel):
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
    policy: ModelDevMergeRedeployPolicy,
    host: str,
    *,
    omni_root: Path,
    home: Path,
    overlay_root: Path,
) -> str:
    """The systemd user service text for ``host``; ``ValueError`` when no unit or host is declared."""
    unit = policy.unit
    if unit is None:
        raise ValueError("the overlay declares no unit block, so no unit is declared")
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
    lane = f" --bus-lane {_quote(unit.bus_lane)}" if unit.bus_lane is not None else ""
    return (
        "# Rendered by the dev-merge-redeploy node (dev-merge-redeploy unit install) from the\n"
        "# deployed overlay; the overlay owns every value, so change the overlay and install\n"
        "# again rather than editing this file. Rollback: dev-merge-redeploy unit uninstall.\n"
        f"[Unit]\nDescription={unit.description}\nStartLimitIntervalSec=0\n"
        "\n[Service]\nType=simple\n"
        + "".join(_assignment(name, value) for name, value in environment.items())
        + f"WorkingDirectory={str(omni_root).replace('%', '%%')}\n"
        f"ExecStart={_quote(python)} -m {NODE_MODULE} serve --bus {_quote(unit.bus)}{lane} "
        f"--host-name {_quote(host)}\n"
        "Restart=on-failure\n"
        f"RestartSec={unit.restart_seconds}\n"
        f"TimeoutStopSec={unit.stop_timeout_seconds}\n"
        "KillSignal=SIGTERM\n"
        f"StandardOutput=append:{log_file}\n"
        "StandardError=inherit\n"
        f"SyslogIdentifier={unit.unit_name}\n"
        "\n[Install]\nWantedBy=default.target\n"
    )


class DevMergeUnitInstaller:
    """Check, install, remove and read the serve unit on one host."""

    def __init__(
        self,
        *,
        policy: ModelDevMergeRedeployPolicy,
        home: Path,
        run: Runner = run_command,
    ) -> None:
        self.policy = policy
        self.home = home
        self.run = run
        self.service_name = (
            f"{policy.unit.unit_name}.service" if policy.unit is not None else ""
        )
        self.unit_dir = home / ".config/systemd/user"

    def _declared(self, host: str) -> ModelDevMergeUnitResult | None:
        if self.policy.unit is None:
            return ModelDevMergeUnitResult(
                exit_code=1,
                stderr="the overlay declares no unit block, so no unit is declared\n",
            )
        if not any(row.name == host for row in self.policy.unit.hosts):
            return ModelDevMergeUnitResult(
                exit_code=1,
                stderr=f"{host}: not in the overlay's unit.hosts, so the unit is not declared for it\n",
            )
        return None

    def render(
        self, host: str, *, omni_root: Path, overlay_root: Path
    ) -> ModelDevMergeUnitResult:
        try:
            text = render_unit(
                self.policy,
                host,
                omni_root=omni_root,
                home=self.home,
                overlay_root=overlay_root,
            )
            refuse_worktree_roots({self.service_name: text})
        except ValueError as exc:
            return ModelDevMergeUnitResult(exit_code=1, stderr=f"{exc}\n")
        return ModelDevMergeUnitResult(exit_code=0, stdout=text)

    def install(
        self,
        host: str,
        *,
        omni_root: Path,
        overlay_root: Path,
        dry_run: bool = False,
    ) -> ModelDevMergeUnitResult:
        rendered = self.render(host, omni_root=omni_root, overlay_root=overlay_root)
        if rendered.exit_code or dry_run:
            return rendered
        unit = self.policy.unit
        if unit is None:
            return ModelDevMergeUnitResult(
                exit_code=1,
                stderr="the overlay declares no unit block, so no unit is declared\n",
            )
        python = Path(_fill(unit.python, omni_root=omni_root, home=self.home))
        if not python.is_file():
            return ModelDevMergeUnitResult(
                exit_code=1, stderr=f"no interpreter at {python}\n"
            )
        probe = self.run(
            [
                "env",
                str(python),
                "-c",
                f"import {NODE_MODULE}.handlers.adapter_dev_merge_redeploy_host",
            ]
        )
        if probe.returncode:
            return ModelDevMergeUnitResult(
                exit_code=1,
                stderr=(
                    f"{python} cannot import the host module (exit {probe.returncode}); "
                    f"the unit would not start\n{probe.stderr}"
                ),
            )
        lane_probe = self.run(list(self.policy.lane_probe_argv))
        if lane_probe.returncode:
            return ModelDevMergeUnitResult(
                exit_code=1,
                stderr=(
                    f"the lane probe exited {lane_probe.returncode}, so the unit would only "
                    f"write deadline_missed receipts; nothing was written\n{lane_probe.stderr}"
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
                return ModelDevMergeUnitResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        return ModelDevMergeUnitResult(
            exit_code=0, stdout=output + f"installed {self.service_name} on {host}\n"
        )

    def uninstall(self, host: str) -> ModelDevMergeUnitResult:
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
            return ModelDevMergeUnitResult(
                exit_code=reloaded.returncode, stdout=output, stderr=reloaded.stderr
            )
        return ModelDevMergeUnitResult(
            exit_code=0, stdout=output + f"uninstalled {self.service_name}\n"
        )

    def status(self, host: str) -> ModelDevMergeUnitResult:
        refused = self._declared(host)
        if refused is not None:
            return refused
        active = self.run(["systemctl", "--user", "is-active", self.service_name])
        enabled = self.run(["systemctl", "--user", "is-enabled", self.service_name])
        return ModelDevMergeUnitResult(
            exit_code=0 if active.returncode == 0 else 1,
            stdout=(
                f"is-active: {active.stdout.strip() or active.stderr.strip()}\n"
                f"is-enabled: {enabled.stdout.strip() or enabled.stderr.strip()}\n"
            ),
        )


__all__ = [
    "DevMergeUnitInstaller",
    "ModelDevMergeUnitResult",
    "render_unit",
    "run_command",
]
