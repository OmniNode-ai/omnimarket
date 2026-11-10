# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read this Mac before onboarding can provision anything.

Floors preserve lab-onboarding's native and optional Docker requirements.
No environment test overrides, profile writes, installation or credentials.
"""

from __future__ import annotations

import errno
import os
import platform
import shutil
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_machine_facts import (
    ModelSetupMachineFacts,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

_GIB = 1024**3
_PORTS = (5436, 19092, 16379, 8085, 8086)


def _command(argv: list[str], *, optional_oid: bool = False) -> str:
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=5, check=False
    )
    if result.returncode:
        if optional_oid and "unknown oid" in result.stderr.lower():
            return "0"
        raise OSError("Machine probe did not complete")
    if len(result.stdout) > 4096:
        raise ValueError("Machine probe returned excessive data")
    return result.stdout.strip()


def collect_machine_facts(workspace: Path) -> ModelSetupMachineFacts:
    ancestor = workspace
    while not ancestor.exists():
        ancestor = ancestor.parent
    disk = min(shutil.disk_usage(ancestor).free, shutil.disk_usage(Path.home()).free)
    system = platform.system()
    if system != "Darwin":
        return ModelSetupMachineFacts(
            platform=system,
            architecture=platform.machine(),
            macos_version="",
            free_disk_bytes=disk,
        )
    occupied: list[int] = []
    for port in _PORTS:
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            occupied.append(port)
    missing = [
        name for name in ("git", "gh", "jq", "uv", "brew") if shutil.which(name) is None
    ]
    xcode = subprocess.run(
        ["/usr/bin/xcode-select", "-p"], capture_output=True, timeout=5, check=False
    )
    if xcode.returncode:
        missing.append("xcode-command-line-tools")
    stack = False
    docker = shutil.which("docker")
    if docker is not None:
        result = subprocess.run(
            [
                docker,
                "ps",
                "-q",
                "--filter",
                "label=com.docker.compose.project=omnibase-infra-local",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        stack = result.returncode == 0 and bool(result.stdout.strip())
    return ModelSetupMachineFacts(
        platform=system,
        architecture=platform.machine(),
        macos_version=platform.mac_ver()[0],
        cpus=os.cpu_count(),
        ram_bytes=int(_command(["/usr/sbin/sysctl", "-n", "hw.memsize"])),
        free_disk_bytes=disk,
        administrator="admin" in _command(["/usr/bin/id", "-Gn"]).split(),
        virtual_machine=_command(
            ["/usr/sbin/sysctl", "-n", "kern.hv_vmm_present"], optional_oid=True
        )
        == "1",
        rosetta=_command(
            ["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"], optional_oid=True
        )
        == "1",
        docker_installed=Path("/Applications/Docker.app").is_dir(),
        stack_running=stack,
        occupied_ports=tuple(occupied),
        missing_tools=tuple(missing),
    )


class HandlerSetupPreflight:
    """Canonical observation of actual machine suitability before side effects."""

    name = "preflight"

    def __init__(
        self,
        *,
        collect: Callable[[Path], ModelSetupMachineFacts] | None = None,
        require_tools: bool = True,
    ) -> None:
        self._collect = collect or collect_machine_facts
        self._require_tools = require_tools

    def handle(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        try:
            facts = self._collect(plan.workspace)
            evidence = {
                key: str(value)
                for key, value in facts.model_dump().items()
                if value is not None and key not in ("occupied_ports", "missing_tools")
            }
            evidence["occupied_ports"] = ",".join(map(str, facts.occupied_ports))
            evidence["missing_tools"] = ",".join(facts.missing_tools)
            reasons: list[str] = []
            if facts.platform != "Darwin":
                reasons.append("Run onboarding on macOS 14 or newer.")
            else:
                if int(facts.macos_version.split(".")[0]) < 14:
                    reasons.append("Upgrade to macOS 14 or newer.")
                if facts.architecture not in ("arm64", "x86_64"):
                    reasons.append("Use a supported arm64 or x86_64 Mac architecture.")
                if facts.rosetta:
                    reasons.append("Open a native arm64 terminal instead of Rosetta.")
                if not facts.administrator:
                    reasons.append("Run from an administrator account.")
                docker = "docker" in plan.components
                cpus, ram, disk = (
                    (8, 16, 20 if facts.docker_installed else 30)
                    if docker
                    else (4, 8, 10)
                )
                if facts.cpus is None or facts.ram_bytes is None:
                    raise ValueError("Missing hardware reading")
                if facts.cpus < cpus:
                    reasons.append(f"Use a Mac with at least {cpus} CPU cores.")
                if facts.ram_bytes < ram * _GIB:
                    reasons.append(f"Use a Mac with at least {ram} GiB RAM.")
                if facts.free_disk_bytes < disk * _GIB:
                    reasons.append(
                        f"Free disk space: {facts.free_disk_bytes / _GIB:.1f} GiB available; at least {disk} GiB required."
                    )
                if docker and facts.virtual_machine:
                    reasons.append(
                        "Docker Desktop requires a physical Mac; use --no-containers in a macOS VM."
                    )
                if docker and facts.occupied_ports and not facts.stack_running:
                    reasons.append(
                        "Resolve occupied Docker ports: "
                        + evidence["occupied_ports"]
                        + "."
                    )
            if reasons:
                return ModelSetupObservation(
                    step=self.name,
                    status="action_required"
                    if facts.rosetta or facts.administrator is False
                    else "unavailable",
                    evidence=evidence,
                    fix=" ".join(reasons),
                )
            if facts.missing_tools and self._require_tools:
                return ModelSetupObservation(
                    step=self.name,
                    status="unavailable",
                    evidence=evidence,
                    fix="Install base tools: " + evidence["missing_tools"] + ".",
                )
            return ModelSetupObservation(
                step=self.name, status="success", evidence=evidence
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return ModelSetupObservation(
                step=self.name,
                status="failure",
                fix="A machine probe could not complete. Run onex setup --preflight-only from a native macOS Terminal and check tool access.",
            )

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        return self.handle(plan)

    def apply(self, plan: ModelSetupPlan) -> None:
        # Machine requirements cannot be repaired by an installation effect.
        raise ValueError("Resolve the preflight observation before provisioning")
