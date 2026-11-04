# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host I/O retained by the migrated alarm, without importing another effect's internals."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

CONTROLLER_STALE = timedelta(minutes=20)


def one_line(value: object, fallback: str) -> str:
    return (
        " ".join(str(value).replace("\r", " ").replace("\n", " ").split()) or fallback
    )


@dataclass(frozen=True)
class ControllerActivity:
    at: datetime

    def last_activity(self) -> datetime:
        return self.at


def read_controller(state_dir: Path) -> ControllerActivity | None:
    """Keep the receipt, completion and in-flight heartbeat liveness semantics."""
    try:
        receipts = state_dir / "ticks.jsonl"
        payload = json.loads(receipts.read_text(encoding="utf-8").splitlines()[-1])
        stamp = datetime.fromisoformat(payload["ts"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or not all(
            isinstance(payload.get(k), str) for k in ("mode", "status")
        ):
            return None
        if payload.get("refusal") is not None and not isinstance(
            payload["refusal"], str
        ):
            return None
        if not isinstance(payload.get("detail", ""), str):
            return None
        latest = max(stamp, datetime.fromtimestamp(receipts.stat().st_mtime, UTC))
        try:
            heartbeat = datetime.fromisoformat(
                (state_dir / "heartbeat").read_text().strip().replace("Z", "+00:00")
            )
            if heartbeat.tzinfo is not None:
                latest = max(latest, heartbeat)
        except (OSError, ValueError):
            pass
        return ControllerActivity(latest)
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None


def append_row(
    python: str, ledger_lock: str | None, ledger: Path, row: str, *, registry_root: Path
) -> tuple[bool, str]:
    """Use the installed canonical appender; an explicit script supports hermetic replay."""
    command = [python, ledger_lock] if ledger_lock else ["onex-ledger"]
    try:
        done = subprocess.run(
            [*command, str(ledger), "--append", row],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            cwd=registry_root,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, one_line(exc, "ledger append could not run")
    lines = (done.stderr + "\n" + done.stdout).strip().splitlines()
    return done.returncode == 0, one_line(
        lines[-1] if lines else "",
        "appended" if done.returncode == 0 else "append failed",
    )
