# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lock-holder process identification and termination helpers."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal


class ReconcileStopError(Exception):
    def __init__(self, code: Literal[3, 4, 5, 6], detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _live(pid: str) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def read_holder(lock: Path) -> dict[str, str]:
    try:
        return dict(
            line.split("=", 1)
            for line in (lock / "holder").read_text().splitlines()
            if "=" in line
        )
    except OSError:
        return {}


def _process_info(pid: int) -> tuple[str, str, str] | None:
    try:
        # A set COLUMNS truncates the command column, hiding the name it is matched on.
        result = subprocess.run(
            [
                "ps",
                "-ww",
                "-o",
                "stat=",
                "-o",
                "etime=",
                "-o",
                "command=",
                "-p",
                str(pid),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={k: v for k, v in os.environ.items() if k != "COLUMNS"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not result.stdout.strip():
        return ("", "", "")
    cells = result.stdout.strip().split(None, 2)
    if result.returncode or len(cells) != 3:
        return None
    return cells[0], cells[1], cells[2]


def _elapsed_matches(elapsed: str, started_at: str) -> bool:
    try:
        started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        days, separator, clock = elapsed.partition("-")
        parts = (clock if separator else days).split(":")
        if len(parts) not in (2, 3):
            return False
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + int(part)
        if separator:
            seconds += int(days) * 86400
        return seconds >= time.time() - started.timestamp() - 5
    except ValueError:
        return False


def confirm_reconcile_holder(fields: dict[str, str], host: str) -> tuple[bool, str]:
    if fields.get("holder", "host-reconcile") != "host-reconcile":
        return False, f"holder role is {fields.get('holder')}, not host-reconcile"
    pid = fields.get("pid", "")
    if fields.get("host") != host or not pid.isdigit() or int(pid) <= 0:
        return False, "holder host or pid does not identify a local process"
    info = _process_info(int(pid))
    if info is None or not info[0] or info[0].startswith("Z"):
        return False, "ps did not confirm a running holder"
    state, elapsed, command = info
    if "host_reconcile" not in command and "onex-host-reconcile" not in command:
        return False, "holder command does not identify host_reconcile"
    if not _elapsed_matches(elapsed, fields.get("started_at", "")):
        return (
            False,
            "holder elapsed time does not match started_at; pid reuse cannot be excluded",
        )
    return (
        True,
        f"ps confirmed host_reconcile pid {pid} in state {state} with elapsed time {elapsed}",
    )


def _gone(pid: int) -> bool:
    if not _live(str(pid)):
        return True
    info = _process_info(pid)
    return info is not None and (not info[0] or info[0].startswith("Z"))


def _wait_gone(pid: int, grace_s: float) -> bool:
    deadline = time.monotonic() + grace_s
    while True:
        if _gone(pid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))


def _signal_holder(pid: int, signum: int, *, group: bool) -> None:
    try:
        if group:
            os.killpg(pid, signum)
        else:
            os.kill(pid, signum)
    except ProcessLookupError:
        pass


def terminate_holder(fields: dict[str, str], grace_s: float = 10.0) -> str:
    pid = int(fields["pid"])
    host = fields["host"]
    try:
        group = os.getpgid(pid) == pid
    except ProcessLookupError:
        group = False
    ended_by = "SIGTERM"
    _signal_holder(pid, signal.SIGTERM, group=group)
    if not _wait_gone(pid, grace_s):
        ended_by = "SIGKILL"
        _signal_holder(pid, signal.SIGKILL, group=group)
        if not _wait_gone(pid, grace_s):
            raise ReconcileStopError(
                6,
                f"STALE-LIVE-HOLDER: pid {pid} on {host} survived SIGKILL; the lock was retained",
            )
    child_signalled = False
    child = fields.get("child_pgid", "")
    if child.isdigit() and int(child) > 0 and fields.get("child_started_at"):
        child_pid = int(child)
        try:
            child_group = os.getpgid(child_pid) == child_pid
        except ProcessLookupError:
            child_group = False
        info = _process_info(child_pid) if child_group else None
        if info and info[0] and _elapsed_matches(info[1], fields["child_started_at"]):
            child_signalled = True
            _signal_holder(child_pid, signal.SIGTERM, group=True)
            if not _wait_gone(child_pid, grace_s):
                _signal_holder(child_pid, signal.SIGKILL, group=True)
    child_detail = (
        "a child group was signalled"
        if child_signalled
        else "no child group was signalled"
    )
    return f"{ended_by} ended holder pid {pid}; {child_detail}"
