# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports and real adapters for the host retention node.

Every external effect (docker, uv, the filesystem and the lane liveness scan) is behind a port so
that tests inject fakes. The real adapters shell out with an argv list (never ``shell=True``),
capture output and time out after 120 seconds.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelDiskReading,
)


def _run(
    argv: list[str], *, timeout_s: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one argv; a missing binary or a timeout is an OSError, never a crash of the pass."""
    try:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise OSError(f"{argv[0]} {argv[1]}: no answer in {timeout_s:.0f} s") from None
    except FileNotFoundError:
        raise OSError(f"{argv[0]}: not installed") from None


class CiHostStatePort(Protocol):
    """Whether a runner container is idle."""

    def is_idle(self, container: str) -> bool | None:  # None = state could not be read
        ...


class CiHostRemovePort(Protocol):
    """Remove a path under the home of a runner container user."""

    def remove(
        self, container: str, user: str, home_relative_path: str
    ) -> None:  # raises OSError on failure
        ...


class UvCachePort(Protocol):
    """Measure and prune a uv cache."""

    def size_bytes(self, uv_binary: str, cache_dir: str, timeout_s: float) -> int: ...

    def prune(
        self, uv_binary: str, cache_dir: str, ci: bool, timeout_s: float
    ) -> None:  # raises OSError on failure
        ...


class LaneLivenessPort(Protocol):
    """Which lanes are live."""

    def live_lanes(
        self, run_roots: Sequence[str], receipt_roots: Sequence[str]
    ) -> set[str] | None:  # None = unreadable
        ...


class ContainerMountPort(Protocol):
    """The host paths that running containers bind-mount."""

    def mount_sources(self) -> set[str] | None:  # None = unreadable
        ...


class UnitFilesPort(Protocol):
    """The text of every installed unit file under the declared unit directories."""

    def unit_texts(self, roots: Sequence[str]) -> list[str] | None:  # None = unreadable
        ...


class DiskPort(Protocol):
    """Read disk usage for a path."""

    def reading(self, path: str) -> ModelDiskReading: ...


class DockerCiHostState:
    """A runner is idle when no ``Runner.Worker`` process is running in the container."""

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    def is_idle(self, container: str) -> bool | None:
        try:
            proc = _run(
                ["docker", "top", container, "-eo", "pid,args"],
                timeout_s=self.timeout_s,
            )
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        return all("Runner.Worker" not in line for line in proc.stdout.splitlines())


class DockerCiHostRemove:
    """Remove one path under a runner container user's home, as that user, after refusing unsafe paths.

    The path is passed to ``sh`` as an argument (never spliced into the script) and joined to ``$HOME`` inside the container.
    """

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    def remove(self, container: str, user: str, home_relative_path: str) -> None:
        path = home_relative_path
        parts = [part for part in path.split("/") if part]
        if not path or path.startswith("/"):
            raise OSError(f"{path!r} is not a path relative to the home")
        if os.path.normpath(path) != path or ".." in parts:
            raise OSError(f"{path!r} is not normalised or has a '..' component")
        if len(parts) < 2:
            raise OSError(f"{path!r} has fewer than two components")
        proc = _run(
            [
                "docker",
                "exec",
                "--user",
                user,
                container,
                "sh",
                "-c",
                'rm -rf -- "$HOME/$1"',
                "sh",
                path,
            ],
            timeout_s=self.timeout_s,
        )
        if proc.returncode != 0:
            raise OSError(proc.stderr[:200])


class ProcessUvCache:
    """Measure and prune a uv cache with the ``uv`` binary."""

    def size_bytes(self, uv_binary: str, cache_dir: str, timeout_s: float) -> int:
        env = {**os.environ, "UV_CACHE_DIR": cache_dir}
        proc = _run([uv_binary, "cache", "size"], timeout_s=timeout_s, env=env)
        if proc.returncode != 0:
            raise OSError(proc.stderr[:200])
        match = re.search(r"\d+", proc.stdout)
        if not match:
            raise OSError("no integer in uv cache size output")
        return int(match.group(0))

    def prune(self, uv_binary: str, cache_dir: str, ci: bool, timeout_s: float) -> None:
        env = {**os.environ, "UV_CACHE_DIR": cache_dir}
        argv = [uv_binary, "cache", "prune"]
        if ci:
            argv.append("--ci")
        proc = _run(argv, timeout_s=timeout_s, env=env)
        if proc.returncode != 0:
            raise OSError(proc.stderr[:200])


class FilesystemLaneLiveness:
    """A lane is live when a run context holds a running pid, or a receipt is not final."""

    def live_lanes(
        self, run_roots: Sequence[str], receipt_roots: Sequence[str]
    ) -> set[str] | None:
        lanes: set[str] = set()
        for root in run_roots:
            path = Path(root)
            if not path.is_dir():
                continue
            try:
                entries = list(path.iterdir())
            except OSError:
                return None
            for entry in entries:
                name = entry.name
                if not name.startswith("rlane-"):
                    continue
                body = name[len("rlane-") :]
                if body.endswith(".ctx"):
                    body = body[:-4]
                if len(body) < 8 or body[-7] != "-":
                    continue
                if not all(c in "0123456789abcdef" for c in body[-6:]):
                    continue
                lane = body[:-7]
                pid_file = entry / "run.pid"
                if not pid_file.is_file():
                    continue
                try:
                    pid = int(pid_file.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    continue
                try:
                    os.kill(pid, 0)
                except PermissionError:
                    pass
                except OSError:
                    continue
                lanes.add(lane)
        for root in receipt_roots:
            path = Path(root)
            if not path.is_dir():
                continue
            try:
                entries = list(path.iterdir())
            except OSError:
                return None
            for entry in entries:
                if not entry.is_dir():
                    continue
                lane = entry.name
                try:
                    receipts = list(entry.glob("*.json"))
                except OSError:
                    return None
                for receipt in receipts:
                    try:
                        document = json.loads(receipt.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if isinstance(document, dict) and document.get("final") is False:
                        lanes.add(lane)
                        break
        return lanes


class DockerContainerMounts:
    """Every bind-mount source of every running container, read with ``docker ps`` and ``docker inspect``."""

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    def mount_sources(self) -> set[str] | None:
        try:
            listing = _run(["docker", "ps", "-q"], timeout_s=self.timeout_s)
        except OSError:
            return None
        if listing.returncode != 0:
            return None
        ids = listing.stdout.split()
        if not ids:
            return set()
        try:
            inspected = _run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{range .Mounts}}{{.Source}}{{println}}{{end}}",
                    *ids,
                ],
                timeout_s=self.timeout_s,
            )
        except OSError:
            return None
        if inspected.returncode != 0:
            return None
        return {line for line in inspected.stdout.splitlines() if line.startswith("/")}


class FilesystemUnitFiles:
    """Unit files and their drop-ins, read as text; a declared directory that exists but cannot be read is unreadable."""

    SUFFIXES = (".service", ".timer", ".path", ".socket", ".mount", ".conf")

    def unit_texts(self, roots: Sequence[str]) -> list[str] | None:
        texts: list[str] = []
        for root in roots:
            base = Path(root)
            if not base.is_dir():
                continue
            try:
                for current, _dirs, files in os.walk(base, followlinks=False):
                    for name in files:
                        if name.endswith(self.SUFFIXES):
                            with contextlib.suppress(OSError, UnicodeError):
                                texts.append(
                                    (Path(current) / name).read_text(encoding="utf-8")
                                )
            except OSError:
                return None
        return texts


class ShutilDisk:
    """Disk usage via ``shutil.disk_usage``."""

    def reading(self, path: str) -> ModelDiskReading:
        usage = shutil.disk_usage(path)
        return ModelDiskReading(
            path=path,
            used_bytes=usage.used,
            total_bytes=usage.total,
            used_percent=100.0 * usage.used / usage.total if usage.total else 0.0,
        )


@dataclass(frozen=True)
class HostRetentionPorts:
    """The seven ports a handler needs, with a ``default()`` of the real adapters."""

    runner_state: CiHostStatePort
    runner_remove: CiHostRemovePort
    uv_cache: UvCachePort
    lane_liveness: LaneLivenessPort
    container_mounts: ContainerMountPort
    unit_files: UnitFilesPort
    disk: DiskPort

    @classmethod
    def default(cls, docker_timeout_s: float) -> HostRetentionPorts:
        return cls(
            runner_state=DockerCiHostState(docker_timeout_s),
            runner_remove=DockerCiHostRemove(docker_timeout_s),
            uv_cache=ProcessUvCache(),
            lane_liveness=FilesystemLaneLiveness(),
            container_mounts=DockerContainerMounts(docker_timeout_s),
            unit_files=FilesystemUnitFiles(),
            disk=ShutilDisk(),
        )
