# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads this host's identity, GPUs, runtimes, software and served models (OMN-19998).

The one place the host introspection node reads the machine. Every value the overlay asks for is read
through an injected seam; a reading that cannot be taken raises :class:`HostFactsUnreadableError` and
yields no facts, so a reader downstream never takes a zero-filled host for a real one.
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from omnimarket.models.model_host_introspection_announcement import (
    EnumSoftwareKind,
    ModelHostFacts,
    ModelHostGpu,
    ModelHostIdentity,
    ModelHostModelServed,
    ModelHostRuntime,
    ModelHostSoftware,
)
from omnimarket.models.model_host_introspection_request import (
    ModelHostIntrospectionOverlay,
    ModelHostReadTargets,
    ModelProbeSpec,
)
from omnimarket.nodes.node_host_introspection_announce_effect.protocols.protocol_host_facts_reader import (
    HostFactsUnreadableError,
)

#: The prefix of the error a probe raises when its command is not installed (the host simply lacks it).
NOT_FOUND_PREFIX = "not found:"


def _default_run(argv: Sequence[str], timeout: float) -> str:
    """Runs ``argv`` with no shell and returns its stdout."""
    expanded = [os.path.expandvars(item) for item in argv]
    try:
        done = subprocess.run(
            expanded, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        raise HostFactsUnreadableError(f"{NOT_FOUND_PREFIX} {expanded[0]}") from None
    except OSError as exc:
        raise HostFactsUnreadableError(str(exc)) from exc
    except subprocess.SubprocessError as exc:
        raise HostFactsUnreadableError(str(exc)) from exc
    if done.returncode != 0:
        raise HostFactsUnreadableError(f"{expanded[0]} exited {done.returncode}")
    return done.stdout


def _default_http_get_json(url: str, timeout: float) -> Any:
    """GETs ``url`` and returns the parsed JSON body."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise HostFactsUnreadableError(f"{url}: {exc}") from exc


def _default_system() -> tuple[str, str, str]:
    """The operating system, architecture and kernel release of this host."""
    return platform.system(), platform.machine(), platform.release()


def _default_hostnames() -> tuple[str, ...]:
    """The hostnames this machine answers to."""
    return (socket.gethostname(),)


def _default_boot_id() -> str | None:
    """The boot id on Linux, or None where the platform gives none."""
    if platform.system() != "Linux":
        return None
    try:
        return (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        )
    except OSError:
        return None


class LocalHostFactsReader:
    """Reads the host this process runs on, through injected seams."""

    def __init__(
        self,
        run: Callable[[Sequence[str], float], str] | None = None,
        http_get_json: Callable[[str, float], Any] | None = None,
        system: Callable[[], tuple[str, str, str]] | None = None,
        hostnames: Callable[[], tuple[str, ...]] | None = None,
        boot_id: Callable[[], str | None] | None = None,
    ) -> None:
        self._run = run if run is not None else _default_run
        self._http_get_json = (
            http_get_json if http_get_json is not None else _default_http_get_json
        )
        self._system = system if system is not None else _default_system
        self._hostnames = hostnames if hostnames is not None else _default_hostnames
        self._boot_id = boot_id if boot_id is not None else _default_boot_id

    def _probe(self, spec: ModelProbeSpec, timeout: float) -> tuple[bool, str | None]:
        """Runs one probe: (installed, value line). Not installed is (False, None); a failure is raised."""
        try:
            output = self._run(spec.argv, timeout)
        except HostFactsUnreadableError as exc:
            if str(exc).startswith(NOT_FOUND_PREFIX):
                return False, None
            raise
        lines = [line for line in output.splitlines() if line.strip()]
        if not lines:
            return True, None
        line = lines[0].strip()
        if spec.value_regex is not None:
            match = re.search(spec.value_regex, line)
            return True, (match.group(1) if match is not None else None)
        return True, line

    def _software_cell(self, spec: ModelProbeSpec | None, timeout: float) -> str | None:
        """One version or commit cell of a software row; a probe that fails leaves the cell unread (None), not the row."""
        if spec is None:
            return None
        try:
            return self._probe(spec, timeout)[1]
        except HostFactsUnreadableError:  # the row is reported with the cell unread
            return None

    def _read_gpus(
        self, overlay: ModelHostIntrospectionOverlay, timeout: float
    ) -> tuple[ModelHostGpu, ...]:
        if not overlay.gpu_query_argv:
            return ()
        try:
            output = self._run(overlay.gpu_query_argv, timeout)
        except HostFactsUnreadableError as exc:
            if str(exc).startswith(NOT_FOUND_PREFIX):
                return ()
            raise
        gpus: list[ModelHostGpu] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 3:
                raise HostFactsUnreadableError(f"gpu line does not parse: {line!r}")
            name = parts[0]
            try:
                total_mib = int(parts[1])
                used_mib = int(parts[2])
            except ValueError:
                raise HostFactsUnreadableError(
                    f"gpu line does not parse: {line!r}"
                ) from None
            gpus.append(
                ModelHostGpu(
                    name=name,
                    mem_total_bytes=total_mib * 1024 * 1024,
                    mem_used_bytes=used_mib * 1024 * 1024,
                )
            )
        return tuple(gpus)

    def _read_runtimes(
        self, overlay: ModelHostIntrospectionOverlay, timeout: float
    ) -> tuple[ModelHostRuntime, ...]:
        runtimes: list[ModelHostRuntime] = []
        for spec in overlay.runtimes:
            installed, version = self._probe(spec, timeout)
            if installed:
                runtimes.append(ModelHostRuntime(name=spec.name, version=version))
        return tuple(runtimes)

    def _read_software(
        self,
        overlay: ModelHostIntrospectionOverlay,
        targets: ModelHostReadTargets,
        timeout: float,
    ) -> tuple[ModelHostSoftware, ...]:
        software: list[ModelHostSoftware] = []
        for probe in overlay.software:
            version = self._software_cell(probe.version, timeout)
            commit = self._software_cell(probe.commit, timeout)
            software.append(
                ModelHostSoftware(
                    name=probe.name, kind=probe.kind, version=version, commit=commit
                )
            )
        for clone in targets.clones:
            clone_commit: str | None = None
            if overlay.clone_commit_argv:
                argv = tuple(
                    item.replace("{path}", clone.path)
                    for item in overlay.clone_commit_argv
                )
                try:
                    output = self._run(argv, timeout)
                    lines = [line for line in output.splitlines() if line.strip()]
                    clone_commit = lines[0].strip() if lines else None
                except (
                    HostFactsUnreadableError
                ):  # the clone is reported with its commit unread
                    clone_commit = None
            software.append(
                ModelHostSoftware(
                    name=clone.name,
                    kind=EnumSoftwareKind.CLONE,
                    version=None,
                    commit=clone_commit,
                )
            )
        return tuple(software)

    def _read_models_served(
        self,
        overlay: ModelHostIntrospectionOverlay,
        targets: ModelHostReadTargets,
        timeout: float,
    ) -> tuple[ModelHostModelServed, ...]:
        if targets.model_port is None:
            return ()
        endpoint = overlay.model_endpoint_template.format(
            host=targets.host_name, port=targets.model_port
        )
        url = f"{endpoint}{overlay.model_list_path}"
        body = self._http_get_json(url, timeout)
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise HostFactsUnreadableError("model list has no data list")
        models: list[ModelHostModelServed] = []
        for entry in body["data"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                raise HostFactsUnreadableError("model entry has no id")
            models.append(
                ModelHostModelServed(
                    name=entry["id"], port=targets.model_port, endpoint=endpoint
                )
            )
        return tuple(models)

    def read(
        self, overlay: ModelHostIntrospectionOverlay, targets: ModelHostReadTargets
    ) -> ModelHostFacts:
        """Reads the host's facts for the overlay and targets."""
        os_name, arch, kernel = self._system()
        identity = ModelHostIdentity(
            hostnames=self._hostnames(),
            os=os_name,
            arch=arch,
            kernel=kernel,
            boot_id=self._boot_id(),
        )
        timeout = overlay.probe_timeout_seconds
        gpus = self._read_gpus(overlay, timeout)
        runtimes = self._read_runtimes(overlay, timeout)
        software = self._read_software(overlay, targets, timeout)
        models_served = self._read_models_served(overlay, targets, timeout)
        return ModelHostFacts(
            identity=identity,
            gpus=gpus,
            models_served=models_served,
            runtimes=runtimes,
            software=software,
        )
