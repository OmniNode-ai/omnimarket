# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the facts of the host this process runs on (OMN-24367).

Graphics cards come from ``nvidia-smi`` when it is installed (a host with no such program has none; one whose
program fails is unreadable, never "no cards"). Runtimes and installed software come from each program's own
``--version``; a program that is absent is left out and one that is present but prints nothing readable carries
an empty version. A commit is reported only when ``--version`` prints one, so a host never announces a commit
it did not read.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from omnimarket.nodes.node_host_introspection_announce_effect.protocols.protocol_host_facts_reader import (
    HostFactsReading,
    HostFactsUnreadableError,
)
from omnimarket.nodes.node_host_introspection_compute.models.model_host_announcement import (
    ModelHostGpu,
    ModelHostLaneAccount,
    ModelHostRuntime,
    ModelHostSoftware,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_host_reader import (
    _claude_logged_in,
    _linux_mem_total,
    _search_path,
)

RUNTIMES: tuple[str, ...] = ("docker", "podman", "python3", "node", "uv", "kubectl")
_VERSION_TIMEOUT_S = 10.0
#: A reading is reused for this long: versions change when something is installed, and the announcer reads once per
#: capacity beat (every ten seconds), which would spawn a dozen processes a beat for facts that move rarely.
FACTS_TTL_SECONDS = 60.0
_VERSION = re.compile(r"\d+(?:\.\d+)+[\w.+-]*")
_COMMIT = re.compile(r"\b(?:commit\s+|\()([0-9a-f]{7,40})\b")


def _run(argv: list[str], path: str) -> str:
    done = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_VERSION_TIMEOUT_S,
        env={**os.environ, "PATH": path},
        check=False,
    )
    return f"{done.stdout}\n{done.stderr}" if done.returncode == 0 else ""


def version_and_commit(program: str, path: str) -> tuple[str, str] | None:
    """The program's version and commit as its own ``--version`` prints them; None when it is not installed."""
    found = shutil.which(program, path=path)
    if found is None:
        return None
    try:
        text = _run([found, "--version"], path)
    except (OSError, subprocess.SubprocessError):
        return ("", "")
    version = _VERSION.search(text)
    commit = _COMMIT.search(text)
    return (version.group(0) if version else "", commit.group(1) if commit else "")


class LocalHostFactsReader:
    """Reads this machine."""

    def __init__(
        self,
        home: Path | None = None,
        now: Callable[[], float] = time.monotonic,
        ttl_seconds: float = FACTS_TTL_SECONDS,
    ) -> None:
        self._home = home
        self._now = now
        self._ttl = ttl_seconds
        self._cached: tuple[float, tuple[str, ...], HostFactsReading] | None = None

    def read(self, software: list[str]) -> HostFactsReading:
        """The host's facts; a good reading is reused for the TTL, a failed one never is."""
        key = tuple(sorted(set(software)))
        stamp = self._now()
        if (
            self._cached is not None
            and self._cached[1] == key
            and stamp - self._cached[0] < self._ttl
        ):
            return self._cached[2]
        reading = self._read(list(key))
        self._cached = (stamp, key, reading)
        return reading

    def _read(self, software: list[str]) -> HostFactsReading:
        cores = os.cpu_count() or 0
        if cores <= 0:
            raise HostFactsUnreadableError("cpu count is unknown")
        path = _search_path()
        try:
            mem_total: int | None = (
                _linux_mem_total() if os.uname().sysname == "Linux" else None
            )
        except (OSError, ValueError) as exc:
            raise HostFactsUnreadableError(f"total memory: {exc}") from exc
        runtimes = tuple(
            ModelHostRuntime(name=name, version=read[0])
            for name in RUNTIMES
            if (read := version_and_commit(name, path)) is not None
        )
        installed = tuple(
            ModelHostSoftware(name=name, version=read[0], commit=read[1])
            for name in sorted(set(software))
            if (read := version_and_commit(name, path)) is not None
        )
        return HostFactsReading(
            cores=cores,
            mem_total_bytes=mem_total,
            gpus=self._gpus(path),
            runtimes=runtimes,
            software=installed,
            lane_accounts=(
                ModelHostLaneAccount(
                    name=getpass.getuser(), signed_in=_claude_logged_in(path)
                ),
            ),
        )

    def _gpus(self, path: str) -> tuple[ModelHostGpu, ...]:
        smi = shutil.which("nvidia-smi", path=path)
        if smi is None:
            return ()
        try:
            done = subprocess.run(
                [smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=_VERSION_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HostFactsUnreadableError(f"nvidia-smi: {exc}") from exc
        if done.returncode != 0:
            raise HostFactsUnreadableError(
                f"nvidia-smi exited {done.returncode}: {done.stderr.strip()[:200]}"
            )
        cards: list[ModelHostGpu] = []
        for line in done.stdout.splitlines():
            name, _, memory = (part.strip() for part in line.partition(","))
            if name:
                cards.append(
                    ModelHostGpu(
                        name=name,
                        memory_total_mib=int(memory) if memory.isdigit() else None,
                    )
                )
        return tuple(cards)
