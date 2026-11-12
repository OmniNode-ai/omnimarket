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

import hashlib
import os
import platform
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from omnimarket.handlers.runtime_paths import runtime_path_list

_EXTRA_PATH = runtime_path_list("lab_work_unit.extra_path_dirs")
#: A pseudo-tool: present only when ``claude auth status`` reports ``loggedIn: true``. A binary on
#: the PATH is not a login (a Mac's keychain is locked over ssh and the same binary reports logged
#: out), so a headless Claude unit asks for this, never for ``claude``.
CLAUDE_LOGIN_TOOL = "claude-login"
_LOGIN_TTL_SECONDS = 300.0
#: A pseudo-tool ``lane-guard:<sha256>`` for each lane guard installed on this host whose bytes still hash to
#: the sha256 in its directory name (``install_lane_guard.py install-guard`` writes
#: ``~/.local/share/onex-lane-guard/sha256/<sha256>/lane_guard_host.py``). A bus unit runs a stub that execs
#: that exact copy and exits 78 LANE-GUARD-MISSING when it is absent or altered, so placement reads this to
#: refuse a host that would fail the unit in seconds (OMN-17427: h203 had none and failed at least seven
#: units in 0 s while staying the freest host). It rides ``tools`` and not a new field, so a caller on an older
#: omnimarket still parses the advertisement instead of dropping the host.
LANE_GUARD_TOOL_PREFIX = "lane-guard:"
#: A pseudo-tool ``cpu-temp-c:<degrees>`` carrying the CPU package temperature (k10temp Tctl, or coretemp Package id)
#: of a Linux host with such a sensor, one decimal. It rides ``tools`` for the same reason the lane guard does: a
#: caller on an older omnimarket still parses the advertisement. A host with no sensor (a Mac, a VM) advertises none
#: and is never judged on temperature. Pool placement reads it against the host row's ``max_cpu_c`` bar.
CPU_TEMP_TOOL_PREFIX = "cpu-temp-c:"
#: A pseudo-tool ``disk-watermark:page`` that a host advertises while its watermark reading file reads ``page`` (OMN-23390):
#: a volume is past its high-water mark, which sits above the admission floor, so pool placement refuses new units there
#: before the floor does. A host below the page mark advertises none. It rides ``tools`` for the same reason the lane guard does.
DISK_WATERMARK_PAGE_TOOL = "disk-watermark:page"
_HWMON_ROOT = Path("/sys/class/hwmon")
_CPU_CHIPS = ("k10temp", "coretemp")
_CPU_LABELS = ("Tctl", "Package id")
_GUARD_DIR = Path(".local") / "share" / "onex-lane-guard" / "sha256"
_GUARD_FILE = "lane_guard_host.py"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def cpu_temp_from_tools(tools: Iterable[str]) -> float | None:
    """The CPU temperature an advertisement's ``cpu-temp-c:`` pseudo-tool carries, or None when it carries none."""
    for tool in tools:
        if tool.startswith(CPU_TEMP_TOOL_PREFIX):
            try:
                return float(tool[len(CPU_TEMP_TOOL_PREFIX) :])
            except ValueError:
                return None
    return None


def disk_watermark_paged(tools: Iterable[str]) -> bool:
    """Whether an advertisement carries the ``disk-watermark:page`` pseudo-tool."""
    return DISK_WATERMARK_PAGE_TOOL in tools


def linux_cpu_temp_c(hwmon_root: Path = _HWMON_ROOT) -> float | None:
    """The hottest CPU package reading under ``hwmon_root`` in degrees C, or None when no such sensor is there."""
    readings: list[float] = []
    try:
        chips = sorted(hwmon_root.iterdir())
    except OSError:
        return None
    for chip in chips:
        try:
            if (chip / "name").read_text().strip() not in _CPU_CHIPS:
                continue
            for label_file in sorted(chip.glob("temp*_label")):
                if label_file.read_text().strip().startswith(_CPU_LABELS):
                    value = (
                        chip / label_file.name.replace("_label", "_input")
                    ).read_text()
                    readings.append(int(value.strip()) / 1000.0)
        except (OSError, ValueError):
            continue
    return max(readings) if readings else None


class HostCapacityUnreadableError(RuntimeError):
    """A load, core or memory reading could not be taken."""


@dataclass(frozen=True)
class HostReading:
    cores: int
    load1: float
    mem_available_bytes: int
    tools: tuple[str, ...]
    #: The capacity inputs of OMN-23337; None when the platform gives no reading, and a None is never judged.
    mem_total_bytes: int | None = None
    swap_in_kb_s: float | None = None
    swap_out_kb_s: float | None = None


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


def _linux_mem_total() -> int:
    text = Path("/proc/meminfo").read_text(encoding="utf-8")
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB", text, re.MULTILINE)
    if match is None:
        raise HostCapacityUnreadableError("MemTotal is missing from /proc/meminfo")
    return int(match.group(1)) * 1024


def _darwin_mem_total() -> int:
    done = subprocess.run(
        ["sysctl", "-n", "hw.memsize"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if done.returncode != 0:
        raise HostCapacityUnreadableError(f"sysctl hw.memsize exited {done.returncode}")
    return int(done.stdout.strip())


def _linux_swap_pages() -> tuple[int, int] | None:
    """The cumulative pages swapped in and out, or None where ``/proc/vmstat`` has no counters."""
    try:
        text = Path("/proc/vmstat").read_text(encoding="utf-8")
    except OSError:
        return None
    found = {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"^(pswpin|pswpout) (\d+)$", text, re.MULTILINE)
    }
    if "pswpin" not in found or "pswpout" not in found:
        return None
    return found["pswpin"], found["pswpout"]


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


def installed_lane_guards(home: Path | None = None) -> tuple[str, ...]:
    """The sha256 of every lane guard installed under ``home`` (default: the user's home) whose bytes hash to
    the sha256 that names its directory. A missing directory, an unreadable file or an altered copy is left
    out; nothing here raises, because a host with no guard advertises none."""
    root = (home if home is not None else Path.home()) / _GUARD_DIR
    try:
        names = sorted(entry.name for entry in root.iterdir())
    except OSError:
        return ()
    found: list[str] = []
    for name in names:
        if _SHA256_RE.match(name) is None:
            continue
        try:
            data = (root / name / _GUARD_FILE).read_bytes()
        except OSError:
            continue
        if hashlib.sha256(data).hexdigest() == name:
            found.append(name)
    return tuple(found)


class LocalHostReader:
    """Reads the host this process runs on."""

    def __init__(
        self,
        now: Callable[[], float] = time.monotonic,
        home: Path | None = None,
        hwmon_root: Path = _HWMON_ROOT,
    ) -> None:
        self._now = now
        self._home = home
        self._hwmon_root = hwmon_root
        self._login: tuple[float, bool] | None = None
        # The previous beat's swap counters: the rate is the change since the last read, so the first read has none.
        self._swap_seen: tuple[float, int, int] | None = None

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
        try:
            mem_total = _linux_mem_total() if system == "Linux" else _darwin_mem_total()
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise HostCapacityUnreadableError(f"total memory: {exc}") from exc
        swap_in, swap_out = self._swap_rates(system)
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
        guards = tuple(
            f"{LANE_GUARD_TOOL_PREFIX}{sha}"
            for sha in installed_lane_guards(self._home)
        )
        temp = linux_cpu_temp_c(self._hwmon_root) if system == "Linux" else None
        heat = () if temp is None else (f"{CPU_TEMP_TOOL_PREFIX}{temp:.1f}",)
        return HostReading(
            cores=cores,
            load1=load1,
            mem_available_bytes=mem,
            tools=tuple(sorted({*found, *guards, *heat})),
            mem_total_bytes=mem_total,
            swap_in_kb_s=swap_in,
            swap_out_kb_s=swap_out,
        )

    def _swap_rates(self, system: str) -> tuple[float | None, float | None]:
        """Swap-in and swap-out in KB/s since the previous read; None on the first read and off Linux."""
        pages = _linux_swap_pages() if system == "Linux" else None
        if pages is None:
            return None, None
        stamp = self._now()
        previous, self._swap_seen = self._swap_seen, (stamp, *pages)
        if previous is None or stamp <= previous[0]:
            return None, None
        page_kb = os.sysconf("SC_PAGE_SIZE") / 1024
        seconds = stamp - previous[0]
        return (
            max(0, pages[0] - previous[1]) * page_kb / seconds,
            max(0, pages[1] - previous[2]) * page_kb / seconds,
        )


__all__ = [
    "CLAUDE_LOGIN_TOOL",
    "CPU_TEMP_TOOL_PREFIX",
    "DISK_WATERMARK_PAGE_TOOL",
    "LANE_GUARD_TOOL_PREFIX",
    "HostCapacityUnreadableError",
    "HostReading",
    "LocalHostReader",
    "ProtocolHostReader",
    "cpu_temp_from_tools",
    "disk_watermark_paged",
    "installed_lane_guards",
    "linux_cpu_temp_c",
]
