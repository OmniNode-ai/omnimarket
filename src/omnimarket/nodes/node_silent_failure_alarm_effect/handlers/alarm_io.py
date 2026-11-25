# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host I/O retained by the migrated alarm, without importing another effect's internals."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.handlers.ledger_append_via_node import (
    append_row_via_bus,
    request_id_for,
)
from omnimarket.handlers.ledger_rows_via_node import read_ledger_rows_via_node

CONTROLLER_STALE = timedelta(minutes=20)
#: The reader name the rows loader answers this alarm's window under (the reader overlay lists it).
READER = "silent-failure-alarm"
ALARM_LANE = "silent-failure-alarm"
LEDGER_COMMAND_ENV = "ONEX_WORK_LEDGER_COMMAND"
APPEND_NAMESPACE = uuid.UUID("3b0f5a52-6a53-4d3b-9d0e-5f7f4a0c9e21")


def one_line(value: object, fallback: str) -> str:
    return (
        " ".join(str(value).replace("\r", " ").replace("\n", " ").split()) or fallback
    )


@dataclass(frozen=True)
class TickActivity:
    at: datetime

    def last_activity(self) -> datetime:
        return self.at


def read_controller(state_dir: Path) -> TickActivity | None:
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
        return TickActivity(latest)
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None


class BusAlarmLedger:
    """The alarm's ledger over the bus: rows come from the rows loader node, appends from the append node.

    No ledger file is named or opened. A window the loader cannot answer is ``LedgerRowsUnreadableError``,
    never an empty window, and an append that gets no answer is retried under the same request id, so the
    ledger host answers a duplicate and the row lands once.
    """

    def read_rows(self, window_hours: int) -> str:
        return "\n".join(read_ledger_rows_via_node(READER, window_hours))

    def append(self, row: str) -> tuple[bool, str]:
        ok, detail = append_row_via_bus(
            row,
            lane=ALARM_LANE,
            request_id=request_id_for(APPEND_NAMESPACE, row),
            command_env=LEDGER_COMMAND_ENV,
        )
        return ok, one_line(detail, "appended" if ok else "append failed")
