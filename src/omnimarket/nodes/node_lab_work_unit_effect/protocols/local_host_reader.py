# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads this host's cores, load1, available memory and tools (OMN-20105).

The one place the lab work unit node reads the machine. Linux: ``/proc/meminfo``
``MemAvailable``. macOS: ``vm_stat`` free plus inactive plus speculative pages,
the reading ``onex-lab-run`` already uses. Load comes from ``os.getloadavg`` on
both. Any unreadable value raises :class:`HostCapacityUnreadableError`; the
reader never returns a zero-filled reading, because a zero would advertise an
idle host.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_EXTRA_PATH = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
#: A pseudo-tool: present only when ``claude auth status`` reports ``loggedIn: true``. A binary on
#: the PATH is not a login (a Mac's keychain is locked over ssh and the same binary reports logged
#: out), so a headless Claude unit asks for this, never for ``claude``.
CLAUDE_LOGIN_TOOL = "claude-login"
_LOGIN_TTL_SECONDS = 300.0


class HostCapacityUnreadableError(RuntimeError):
    """A load, core or memory reading could not be taken."""


@dataclass(frozen=True)
class HostReading:
    cores: int
    load1: float
    mem_available_bytes: int
    tools: tuple[str, ...]


class ProtocolHostReader(Protocol):
    def read(self, tools: list[str]) -> HostReading: ...


def _search_path() -> str:
    extra = [str(Path(p).expanduser()) for p in _EXTRA_PATH]
    return os.pathsep.join([*extra, os.environ.get("PATH", "")])


def _linux_mem_available() -> int:
    text = Path("/proc/meminfo").read_text(encoding="utf-8")
    match = re.search(r"^MemAvailable:\s+(\d+)\s+kB", text, re.MULTILINE)
    if match is None:
        raise HostCapacityUnreadableError("MemAvailable is missing from /proc/meminfo")
    return int(match.group(1)) * 1024


def _darwin_mem_available() -> int:
    done = subprocess.run(
        ["vm_stat"], capture_output=True, text=True, timeout=10, check=False
    )
    if done.returncode != 0:
        raise HostCapacityUnreadableError(f"vm_stat exited {done.returncode}")
    page = re.search(r"page size of (\d+) bytes", done.stdout)
    counts: dict[str, int] = {}
    for label in ("Pages free", "Pages inactive", "Pages speculative"):
        match = re.search(rf"^{label}:\s+(\d+)\.", done.stdout, re.MULTILINE)
        if match is None:
            raise HostCapacityUnreadableError(f"vm_stat has no {label!r} line")
        counts[label] = int(match.group(1))
    if page is None:
        raise HostCapacityUnreadableError("vm_stat has no page size")
    return sum(counts.values()) * int(page.group(1))


def _claude_logged_in(path: str) -> bool:
    # Reads the login state (claude auth status); runs no inference.
    exe = shutil.which("claude", path=path)  # canonical-inference-ok: login check
    if exe is None:
        return False
    try:
        done = subprocess.run(
            [exe, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return re.search(r'"loggedIn":\s*true', done.stdout) is not None


class LocalHostReader:
    """Reads the host this process runs on."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._login: tuple[float, bool] | None = None

    def _has_login(self, path: str) -> bool:
        # A login changes rarely and the check spawns a process, so it is read at most once per
        # _LOGIN_TTL_SECONDS rather than on every advertisement beat.
        stamp = self._now()
        if self._login is None or stamp - self._login[0] > _LOGIN_TTL_SECONDS:
            self._login = (stamp, _claude_logged_in(path))
        return self._login[1]

    def read(self, tools: list[str]) -> HostReading:
        try:
            load1 = os.getloadavg()[0]
        except OSError as exc:
            raise HostCapacityUnreadableError(f"load average: {exc}") from exc
        cores = os.cpu_count() or 0
        if cores <= 0:
            raise HostCapacityUnreadableError("cpu count is unknown")
        system = platform.system()
        try:
            if system == "Linux":
                mem = _linux_mem_available()
            elif system == "Darwin":
                mem = _darwin_mem_available()
            else:
                raise HostCapacityUnreadableError(f"no memory reader for {system}")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise HostCapacityUnreadableError(f"available memory: {exc}") from exc
        path = _search_path()
        found = tuple(
            sorted(
                tool
                for tool in tools
                if (
                    self._has_login(path)
                    if tool == CLAUDE_LOGIN_TOOL
                    else shutil.which(tool, path=path) is not None
                )
            )
        )
        return HostReading(
            cores=cores, load1=load1, mem_available_bytes=mem, tools=found
        )


__all__ = [
    "CLAUDE_LOGIN_TOOL",
    "HostCapacityUnreadableError",
    "HostReading",
    "LocalHostReader",
    "ProtocolHostReader",
]
