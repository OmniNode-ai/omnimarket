# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host I/O retained by the migrated alarm effect."""

from __future__ import annotations

import json
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from ..models.model_alarm_controller_tick import ModelAlarmControllerTick

CONTROLLER_STALE = timedelta(minutes=20)


def one_line(value: object, fallback: str) -> str:
    return " ".join(str(value).split()) or fallback


def _watcher_stamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def append_row(
    python: str,
    ledger_lock: str | None,
    ledger: Path,
    row: str,
    *,
    registry_root: Path,
    bus_lane: str | None = None,
) -> tuple[bool, str]:
    """Send through the existing ledger bus caller; a queued append is not a delivery receipt."""
    if ledger_lock is None and not bus_lane:
        return (
            False,
            "ledger bus lane is undeclared; set ONEX_WORK_LEDGER_BUS_LANE from the deployed writer",
        )
    command = (
        [python, ledger_lock, str(ledger), "--append", row]
        if ledger_lock
        else [
            python,
            "-m",
            "omnimarket.work_ledger_bus.cli",
            "append",
            "--bus",
            "kafka",
            "--bus-lane",
            str(bus_lane),
            "--omnibase-path",
            str(registry_root),
            "--lane",
            "silent-failure-alarm",
            "--host",
            socket.gethostname(),
            "--request-id",
            str(uuid4()),
            "--rows-file",
            "-",
            "--timeout-s",
            "60",
        ]
    )
    try:
        done = subprocess.run(
            command,
            input=row + "\n",
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, one_line(exc, "ledger append failed")
    if ledger_lock:
        return done.returncode == 0, one_line(
            done.stderr or done.stdout, "append finished"
        )
    try:
        receipt = json.loads(done.stdout.splitlines()[-1])
        accepted = receipt.get("status") in ("accepted", "duplicate", "already-present")
    except (ValueError, IndexError, AttributeError):
        accepted = False
    return done.returncode == 0 and accepted, one_line(
        done.stderr or done.stdout, "append failed"
    )


def read_controller(state_dir: Path) -> ModelAlarmControllerTick | None:
    """Read the last JSON receipt; malformed or missing last lines are unknown."""
    try:
        receipts = state_dir / "ticks.jsonl"
        lines = receipts.read_text(encoding="utf-8").splitlines()
        completed = datetime.fromtimestamp(receipts.stat().st_mtime, tz=UTC)
        if not lines:
            return None
        payload = json.loads(lines[-1])
        if not isinstance(payload, dict):
            return None
        stamp = _watcher_stamp(payload.get("ts"))
        mode = payload.get("mode")
        status = payload.get("status")
        refusal = payload.get("refusal")
        detail = payload.get("detail", "")
        if (
            stamp is None
            or not isinstance(mode, str)
            or not isinstance(status, str)
            or (refusal is not None and not isinstance(refusal, str))
            or not isinstance(detail, str)
        ):
            return None
        previous_unknown = False
        if len(lines) > 1:
            try:
                previous = json.loads(lines[-2])
            except ValueError:
                previous = None
            previous_unknown = (
                isinstance(previous, dict) and previous.get("status") == "UNKNOWN"
            )
        # The controller writes the heartbeat with a tick's start stamp when the tick starts and
        # rewrites it with the receipt's stamp at the end: a newer heartbeat is a tick in flight.
        try:
            heartbeat = _watcher_stamp(
                (state_dir / "heartbeat").read_text(encoding="utf-8").strip()
            )
        except (OSError, UnicodeError):
            heartbeat = None
        running_since = (
            heartbeat if heartbeat is not None and heartbeat > stamp else None
        )
        return ModelAlarmControllerTick(
            ts=stamp,
            mode=mode,
            status=status,
            refusal=refusal,
            detail=detail,
            previous_unknown=previous_unknown,
            completed=completed,
            running_since=running_since,
        )
    except (OSError, UnicodeError, ValueError):
        return None
