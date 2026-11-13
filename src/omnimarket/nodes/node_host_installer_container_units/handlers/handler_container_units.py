# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render, install, inspect and remove the systemd user unit that keeps one declared docker container running."""

from __future__ import annotations

import json
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
    if unit.network is not None and not _SAFE.match(unit.network):
        raise ValueError(
            f"network {unit.network!r} holds a character a unit line cannot carry unquoted"
        )
    docker, name = unit.docker_binary, unit.container
    exec_start_pre = ""
    if unit.network is not None:
        exec_start_pre = (
            f"ExecStartPre=-{docker} network connect {unit.network} {name}\n"
        )
    return (
        f"# Written by node_host_installer_container_units ({unit.ticket}). Re-run it; do not hand-edit.\n"
        f"[Unit]\nDescription=Keep docker container {name} running\n"
        "\n[Service]\nType=simple\n"
        + exec_start_pre
        + f"ExecStart={docker} start -a {name}\n"
        f"ExecStop={docker} stop -t {unit.stop_timeout_seconds} {name}\n"
        f"Restart=always\nRestartSec={unit.restart_seconds}\n"
        "\n[Install]\nWantedBy=default.target\n"
    )


def create_argv(unit: ModelContainerUnit) -> list[str]:
    """The argv for ``docker create`` of a unit that declares how to create its container."""
    if unit.create is None:
        raise ValueError("create is not declared for this unit")
    if unit.network is None:
        raise ValueError("network is required when create is declared")
    argv = [
        unit.docker_binary,
        "create",
        "--name",
        unit.container,
        "--network",
        unit.network,
        "--restart",
        unit.create.restart_policy,
    ]
    for port in unit.published_ports:
        argv += [
            "-p",
            f"{port.host_address}:{port.host_port}:{port.container_port}",
        ]
    for volume in unit.create.volumes:
        argv += ["-v", volume]
    if unit.create.env_file is not None:
        argv += ["--env-file", unit.create.env_file]
    argv.append(unit.create.image)
    argv.extend(unit.create.command)
    return argv


def network_problem(unit: ModelContainerUnit, inspect_stdout: str) -> str | None:
    """The first network problem a running container has against the overlay declaration, else None."""
    if unit.network is None:
        return None
    try:
        documents = json.loads(inspect_stdout)
    except (json.JSONDecodeError, ValueError):
        return "docker inspect printed no container document"
    if not isinstance(documents, list) or not documents:
        return "docker inspect printed no container document"
    doc = documents[0]
    host_config = doc.get("HostConfig", {})
    network_mode = host_config.get("NetworkMode", "")
    if network_mode != unit.network:
        return (
            f"container {unit.container} is on network {network_mode}, "
            f"the overlay declares {unit.network}"
        )
    port_bindings = host_config.get("PortBindings", {})
    for port in unit.published_ports:
        key = f"{port.container_port}/tcp"
        binding = port_bindings.get(key)
        if not binding:
            return (
                f"container {unit.container} does not publish "
                f"{port.host_address}:{port.host_port} to {port.container_port}"
            )
        host_ip, host_port = (
            binding[0].get("HostIp", ""),
            binding[0].get("HostPort", ""),
        )
        if str(host_ip) != str(port.host_address) or str(host_port) != str(
            port.host_port
        ):
            return (
                f"container {unit.container} does not publish "
                f"{port.host_address}:{port.host_port} to {port.container_port}"
            )
    state = doc.get("State", {})
    if state.get("Running"):
        networks = doc.get("NetworkSettings", {}).get("Networks", {})
        if unit.network not in networks:
            return f"container {unit.container} has no attachment to network {unit.network}"
    return None


def running_memory_bytes(inspect_stdout: str) -> tuple[int, int]:
    """The running (Memory, MemorySwap) limits in bytes from a ``docker inspect`` document."""
    try:
        documents = json.loads(inspect_stdout)
    except (json.JSONDecodeError, ValueError):
        raise ValueError("docker inspect printed no container document") from None
    if (
        not isinstance(documents, list)
        or not documents
        or not isinstance(documents[0], dict)
    ):
        raise ValueError("docker inspect printed no container document")
    host_config = documents[0].get("HostConfig", {})
    memory, swap = host_config.get("Memory", 0), host_config.get("MemorySwap", 0)
    return int(memory or 0), int(swap or 0)


def memory_problem(
    unit: ModelContainerUnit, inspect_stdout: str
) -> tuple[int, int] | None:
    """The declared (memory, memory plus swap) in bytes the running container lacks, else None."""
    if unit.memory_gib is None:
        return None
    memory_bytes = unit.memory_gib * 1024**3
    swap_bytes = (unit.memory_swap_gib or unit.memory_gib) * 1024**3
    running_memory, running_swap = running_memory_bytes(inspect_stdout)
    if running_memory == memory_bytes and running_swap == swap_bytes:
        return None
    return memory_bytes, swap_bytes


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
            if unit.create is not None:
                created = self.run(create_argv(unit))
                if created.returncode:
                    return ModelContainerUnitsResult(
                        exit_code=1,
                        stderr=f"create failed (exit {created.returncode}): {created.stderr}\n",
                    )
                found = self.run(
                    [
                        unit.docker_binary,
                        "inspect",
                        "--type",
                        "container",
                        unit.container,
                    ]
                )
                if found.returncode:
                    return ModelContainerUnitsResult(
                        exit_code=1,
                        stderr=f"no container {unit.container} on this host (docker inspect exit {found.returncode}); nothing written\n",
                    )
            else:
                return ModelContainerUnitsResult(
                    exit_code=1,
                    stderr=f"no container {unit.container} on this host (docker inspect exit {found.returncode}); nothing written\n",
                )
        problem = network_problem(unit, found.stdout)
        if problem is not None:
            return ModelContainerUnitsResult(
                exit_code=1,
                stderr=f"{problem}; nothing written; recreate the container from its declared create settings\n",
            )
        # OMN-24208: a declared memory limit is applied in place (docker update only, so the
        # container neither restarts nor is recreated) before the unit is written.
        try:
            memory = memory_problem(unit, found.stdout)
        except ValueError as exc:
            return ModelContainerUnitsResult(
                exit_code=1, stderr=f"{exc}; nothing written\n"
            )
        memory_line = ""
        if memory is not None:
            memory_bytes, swap_bytes = memory
            updated = self.run(
                [
                    unit.docker_binary,
                    "update",
                    "--memory",
                    str(memory_bytes),
                    "--memory-swap",
                    str(swap_bytes),
                    unit.container,
                ]
            )
            if updated.returncode:
                return ModelContainerUnitsResult(
                    exit_code=1,
                    stderr=f"docker update failed (exit {updated.returncode}): {updated.stderr}; nothing written\n",
                )
            memory_line = (
                f"set memory limit of {unit.container} to {unit.memory_gib} GiB, "
                f"memory plus swap {unit.memory_swap_gib} GiB\n"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_text(encoding="utf-8") == text:
            output = f"{service} unchanged\n"
        else:
            target.write_text(text, encoding="utf-8")
            output = f"wrote {target}\n"
        output += memory_line
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
        exit_code = 0 if loaded and answers else 1
        found: subprocess.CompletedProcess[str] | None = None
        if unit.network is not None:
            found = self.run(
                [unit.docker_binary, "inspect", "--type", "container", unit.container]
            )
            problem = network_problem(unit, found.stdout)
            if problem is not None:
                output += f"NETWORK PROBLEM {problem}\n"
                exit_code = 1
            else:
                output += f"NETWORK ATTACHED {unit.network}\n"
        if unit.memory_gib is not None:
            if found is None:
                found = self.run(
                    [
                        unit.docker_binary,
                        "inspect",
                        "--type",
                        "container",
                        unit.container,
                    ]
                )
            try:
                running_memory, _running_swap = running_memory_bytes(found.stdout)
            except ValueError as exc:
                return ModelContainerUnitsResult(
                    exit_code=1, stdout=output + f"MEMORY LIMIT UNREAD {exc}\n"
                )
            if memory_problem(unit, found.stdout) is None:
                output += f"MEMORY LIMIT OK {unit.memory_gib} GiB\n"
            else:
                output += (
                    f"MEMORY LIMIT DIFFERS declared {unit.memory_gib} GiB, "
                    f"running {running_memory} bytes\n"
                )
                exit_code = 1
        return ModelContainerUnitsResult(exit_code=exit_code, stdout=output)

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
