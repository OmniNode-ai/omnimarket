# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Restart-count adapters for node_lab_alarm's container-restarts condition (OMN-23934): docker inspect, the lane manifest bounds, and in-memory doubles."""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_lab_alarm.protocols import (
    ContainerUnreadableError,
    RestartBoundsUnreadableError,
)

__all__ = [
    "InMemoryRestartBounds",
    "InMemoryRestartReader",
    "SshDockerRestartReader",
    "YamlLaneManifestBounds",
]


class InMemoryRestartBounds:
    """Serves the declared restart bounds by container; a non-empty ``unreadable`` makes the read refuse with it."""

    def __init__(
        self, bounds: Mapping[str, int] | None = None, unreadable: str = ""
    ) -> None:
        self.declared = dict(bounds or {})
        self.unreadable = unreadable

    def bounds(self) -> dict[str, int]:
        if self.unreadable:
            raise RestartBoundsUnreadableError(self.unreadable)
        return dict(self.declared)


class InMemoryRestartReader:
    """Reads the fixture ``readings`` (restart count and status) by container; a listed ``unreadable`` container refuses."""

    def __init__(self) -> None:
        self.readings: dict[str, tuple[int, str]] = {}
        self.unreadable: dict[str, str] = {}
        self.calls: list[str] = []

    def read(self, container: str, timeout: float) -> tuple[int, str]:
        self.calls.append(container)
        if container in self.unreadable:
            raise ContainerUnreadableError(self.unreadable[container])
        if container not in self.readings:
            raise ContainerUnreadableError(f"{container} is not a fixture container")
        return self.readings[container]


_INSPECT_FORMAT = "{{.RestartCount}} {{.State.Status}}"


class SshDockerRestartReader:
    """``docker inspect`` of one container, through the overlay's argv prefix: the restart count and the status.

    The prefix reaches docker where the lane runs (ssh to the lab host: the alarm host's own docker daemon cannot
    see the lane's containers). Behind anything but a bare ``docker`` the arguments are shell-quoted, because ssh
    hands the joined text to a remote shell. The command is a fixed argv run without a shell, read only. A
    container that is absent, a docker that does not answer, or an answer that is not ``<count> <status>`` raises
    ``ContainerUnreadableError``; nothing here returns a count it did not read.
    """

    def __init__(self, docker_command: Sequence[str]) -> None:
        self._prefix = list(docker_command)

    def read(self, container: str, timeout: float) -> tuple[int, str]:
        inspect = ["inspect", "-f", _INSPECT_FORMAT, container]
        if self._prefix == ["docker"]:
            argv = [*self._prefix, *inspect]
        else:
            argv = [*self._prefix, *(shlex.quote(part) for part in inspect)]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=timeout
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ContainerUnreadableError(
                f"docker inspect {container} failed: {type(exc).__name__}"
            ) from None
        if result.returncode != 0:
            raise ContainerUnreadableError(
                f"docker inspect {container} exited {result.returncode}: "
                f"{(result.stderr or '').strip()[:200]}"
            )
        fields = (result.stdout or "").split()
        if len(fields) < 2:
            raise ContainerUnreadableError(
                f"docker inspect {container} printed {result.stdout!r}"
            )
        try:
            return int(fields[0]), fields[1]
        except ValueError:
            raise ContainerUnreadableError(
                f"restart count for {container} is not an integer: {fields[0]!r}"
            ) from None


class YamlLaneManifestBounds:
    """The per-service ``restart_bound`` entries of one lane of the lane manifest, shared with the census detector.

    ``manifest_path`` may name ``${VARIABLE}`` references, resolved from ``environ``; one left unresolved refuses
    the read. Every declared bound of every lane is validated, as the census detector does, so a malformed bound
    anywhere is not read as a number. A lane that declares no bound refuses: nothing would be watched.
    """

    def __init__(
        self, manifest_path: str, lane: str, environ: Mapping[str, str]
    ) -> None:
        self._manifest_path = manifest_path
        self._lane = lane
        self._environ = dict(environ)

    def bounds(self) -> dict[str, int]:
        path = self._expanded_path()
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise RestartBoundsUnreadableError(
                f"lane manifest unreadable ({type(exc).__name__})"
            ) from None
        if not isinstance(data, dict) or not isinstance(data.get("lanes"), dict):
            raise RestartBoundsUnreadableError("lane manifest missing 'lanes'")
        lanes: dict[str, Any] = data["lanes"]
        for lane, spec in lanes.items():
            for service in (spec or {}).get("services", []):
                if "restart_bound" in service:
                    bound = service["restart_bound"]
                    if type(bound) is not int or bound < 0:
                        raise RestartBoundsUnreadableError(
                            f"{lane}/{service.get('name')}: restart_bound must be a "
                            "non-negative integer"
                        )
        if self._lane not in lanes:
            raise RestartBoundsUnreadableError(f"unknown census lane {self._lane!r}")
        declared = {
            service["name"]: service["restart_bound"]
            for service in (lanes[self._lane] or {}).get("services", [])
            if "restart_bound" in service
        }
        if not declared:
            raise RestartBoundsUnreadableError(
                f"census lane {self._lane!r} declares no restart_bound"
            )
        return declared

    def _expanded_path(self) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in self._environ:
                raise RestartBoundsUnreadableError(
                    f"lane manifest path names the unset variable {name}"
                )
            return self._environ[name]

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, self._manifest_path)
