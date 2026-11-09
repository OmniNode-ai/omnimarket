# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger adapter of the start-admission log port: the observe log (OMN-23306).

One STATUS row per admission decision, appended through the bus append command, so the log of what
admission would refuse is read where every other reading is read. A decision tagged ``drill`` carries
``drill=1``: a drill never reads as an operational start. A row the ledger did not take is counted and
kept, never dropped: the decision it records has been made and is not undone by a log fault.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.handlers.reading_text import (
    reason_token,
    stamp_text,
)
from omnimarket.models.model_start_admission import ModelStartAdmissionLogRow
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

ROW_SOURCE_CELL = "src=start-admission-observe"


class LedgerStartAdmissionLog:
    """Appends each decision as one ledger row; rows that failed to land are kept for the reader."""

    def __init__(
        self,
        appender: ProtocolReadingAppender,
        *,
        lane: str,
        actor: str,
        host: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._appender = appender
        self._lane = lane
        self._actor = actor
        self._host = host
        self._clock = clock
        self._lock = threading.Lock()
        self._written = 0
        self._failed: list[str] = []

    def append(self, row: ModelStartAdmissionLogRow) -> None:
        now = self._clock().astimezone(UTC)
        cells = [
            f"lane={self._lane}",
            ROW_SOURCE_CELL,
            f"actor={self._actor}",
            "model=none",
            f"host={self._host}",
            f"drill={int(row.drill)}",
            f"path={row.path}",
            f"piece={row.piece}",
            f"mode={row.mode.value}",
            f"outcome={row.outcome.value}",
            f"proceed={int(row.proceed)}",
            f"reason={reason_token(row.reason) if row.reason else 'none'}",
            f"epoch={row.epoch if row.epoch is not None else 'none'}",
            f"attempts={row.attempts}",
            f"step={reason_token(row.step_key)}",
            f"corr={reason_token(row.correlation_id)}",
        ]
        text = f"{stamp_text(now)} | STATUS | " + " | ".join(cells)
        request_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"admission-observe|{row.step_key}")
        )
        outcome = self._appender.append(text + "\n", request_id)
        with self._lock:
            if outcome.ok:
                self._written += 1
            else:
                self._failed.append(f"{row.step_key}:{outcome.status}")

    @property
    def written(self) -> int:
        with self._lock:
            return self._written

    @property
    def failed(self) -> list[str]:
        with self._lock:
            return list(self._failed)
