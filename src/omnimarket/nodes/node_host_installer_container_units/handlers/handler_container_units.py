# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render, install, inspect and remove the systemd user unit that keeps one declared docker container running."""

from __future__ import annotations

import re
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_host_installer_container_units.models.model_container_units import (
    ModelContainerUnit,
    ModelContainerUnitsOverlay,
    ModelContainerUnitsRequest,
    ModelContainerUnitsResult,
)

NODE_NAME = "node_host_installer_container_units"
OVERLAY_ENV = "ONEX_HOST_CONTAINER_UNITS_OVERLAY"
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
PortProbe = Callable[[str, int, int], bool]
_SAFE = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a host command; a missing program reads as exit status 127."""
    try:
        return subprocess.run(
            list(command), capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(command), 127, "", str(exc))


def port_answers(address: str, port: int, wait_seconds: int) -> bool:
    """Whether a TCP connection to ``address:port`` succeeds within ``wait_seconds``."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            with socket.create_connection((address, port), timeout=2):
                return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(1)


def render_unit(unit: ModelContainerUnit) -> str:
    """The unit text. ``docker start -a`` attaches to a running container and starts a stopped one."""
    for what, value in (
        ("docker_binary", unit.docker_binary),
        ("container", unit.container),
    ):
        if not _SAFE.match(value):
            raise ValueError(
                f"{what} {value!r} holds a character a unit line cannot carry unquoted"
            )
    docker, name = unit.docker_binary, unit.container
    return (
        f"# Written by node_host_installer_container_units ({unit.ticket}). Re-run it; do not hand-edit.\n"
        f"[Unit]\nDescription=Keep docker container {name} running\n"
        "\n[Service]\nType=simple\n"
        f"ExecStart={docker} start -a {name}\n"
        f"ExecStop={docker} stop -t {unit.stop_timeout_seconds} {name}\n"
        f"Restart=always\nRestartSec={unit.restart_seconds}\n"
        "\n[Install]\nWantedBy=default.target\n"
    )


def declared_unit(
    overlay: ModelContainerUnitsOverlay, host: str, name: str
) -> ModelContainerUnit:
    rows = [u for u in overlay.units if u.name == name and host in u.hosts]
    if len(rows) != 1:
        raise ValueError(
            f"{host}: {len(rows)} units named {name} in the {NODE_NAME} overlay, so none is declared for it"
        )
    return rows[0]


class HandlerContainerUnits:
    """Install, check and remove one declared container unit; ``run``, ``probe`` and ``home`` are injected."""

    def __init__(
        self,
        *,
        run: Runner = run_command,
        probe: PortProbe = port_answers,
        home: Path | None = None,
    ) -> None:
        self.run = run
        self.probe = probe
        self.home = home if home is not None else Path.home()

    def handle(self, request: ModelContainerUnitsRequest) -> ModelContainerUnitsResult:
        try:
            unit = declared_unit(
                self._overlay(request.overlay_file), request.host_name, request.unit
            )
            text = render_unit(unit)
        except ValueError as exc:
            return ModelContainerUnitsResult(exit_code=1, stderr=f"{exc}\n")
        service = f"{unit.name}.service"
        target = self.home / ".config/systemd/user" / service
        if request.mode == "dry_run":
            return ModelContainerUnitsResult(exit_code=0, stdout=text)
        if request.mode == "status":
            return self._status(unit, service)
        if request.mode == "uninstall":
            disabled = self.run(["systemctl", "--user", "disable", "--now", service])
            target.unlink(missing_ok=True)
            reloaded = self.run(["systemctl", "--user", "daemon-reload"])
            return ModelContainerUnitsResult(
                exit_code=disabled.returncode or reloaded.returncode,
                stdout=f"removed {service}; container {unit.container} left as it was\n",
                stderr=disabled.stderr + reloaded.stderr,
            )
        return self._install(unit, service, target, text)

    def _install(
        self, unit: ModelContainerUnit, service: str, target: Path, text: str
    ) -> ModelContainerUnitsResult:
        found = self.run(
            [unit.docker_binary, "inspect", "--type", "container", unit.container]
        )
        if found.returncode:
            return ModelContainerUnitsResult(
                exit_code=1,
                stderr=f"no container {unit.container} on this host (docker inspect exit {found.returncode}); nothing written\n",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_text(encoding="utf-8") == text:
            output = f"{service} unchanged\n"
        else:
            target.write_text(text, encoding="utf-8")
            output = f"wrote {target}\n"
        for args in (["daemon-reload"], ["enable", "--now", service]):
            done = self.run(["systemctl", "--user", *args])
            output += done.stdout
            if done.returncode:
                return ModelContainerUnitsResult(
                    exit_code=done.returncode, stdout=output, stderr=done.stderr
                )
        if not self.probe(unit.probe_address, unit.probe_port, unit.probe_wait_seconds):
            return ModelContainerUnitsResult(
                exit_code=1,
                stdout=output,
                stderr=f"{unit.probe_address}:{unit.probe_port} does not answer after {unit.probe_wait_seconds} s\n",
            )
        return ModelContainerUnitsResult(
            exit_code=0,
            stdout=output
            + f"installed {service}: {unit.container} answers on {unit.probe_address}:{unit.probe_port}\n",
        )

    def _status(
        self, unit: ModelContainerUnit, service: str
    ) -> ModelContainerUnitsResult:
        active = self.run(["systemctl", "--user", "is-active", service])
        answers = self.probe(unit.probe_address, unit.probe_port, 1)
        loaded = active.returncode == 0
        output = (
            f"{'LOADED' if loaded else 'NOT LOADED'} {service}\n"
            f"{'ANSWERS' if answers else 'SILENT'} {unit.probe_address}:{unit.probe_port}\n"
        )
        return ModelContainerUnitsResult(
            exit_code=0 if loaded and answers else 1, stdout=output
        )

    def _overlay(self, overlay_file: str) -> ModelContainerUnitsOverlay:
        if overlay_file:
            try:
                document = yaml.safe_load(
                    Path(overlay_file).read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                raise ValueError(
                    f"overlay {overlay_file} is unreadable ({type(exc).__name__})"
                ) from None
            return load_structured_overlay(
                NODE_NAME, ModelContainerUnitsOverlay, explicit=document
            )
        return load_structured_overlay(
            NODE_NAME, ModelContainerUnitsOverlay, file_env=OVERLAY_ENV
        )
