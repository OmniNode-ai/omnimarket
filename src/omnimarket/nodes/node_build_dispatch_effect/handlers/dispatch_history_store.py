# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File-backed dispatch-history store for build loop stall detection.

Records per-ticket dispatch history so the build loop can:

1. Detect tickets that remain in Backlog after dispatch (stall)
2. Skip tickets that have been dispatched twice with no forward progress
3. Surface stall events in logs for escalation

The store is a single JSON file at ``$HOME/.onex_state/build-loop-dispatch-history.json``.
Writes are atomic (write-to-temp + rename). Reads tolerate a missing file.

Related: OMN-7774 — build loop stall detection
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_DEFAULT_STORE_PATH: Final[Path] = (
    Path.home() / ".onex_state" / "build-loop-dispatch-history.json"
)

# Environment override so tests and alternate deployments can redirect writes.
_ENV_STORE_PATH: Final[str] = "OMNIMARKET_BUILD_LOOP_DISPATCH_HISTORY"

# A ticket is considered stalled if it remains in Backlog this long after
# its most recent dispatch.
STALL_WINDOW_MINUTES: Final[int] = 30

# After this many failed dispatches with no progress, the ticket is skipped.
MAX_DISPATCH_ATTEMPTS: Final[int] = 2


class ModelDispatchRecord(BaseModel):
    model_config = ConfigDict(frozen=False, extra="forbid")

    ticket_id: str
    first_dispatched_at: datetime
    last_dispatched_at: datetime
    attempt_count: int = Field(ge=0)
    last_correlation_id: str


class DispatchHistoryStore:
    """JSON file-backed dispatch history.

    Not thread-safe. Intended for single-process build loop execution.
    """

    def __init__(self, path: Path | None = None) -> None:
        env_override = os.environ.get(_ENV_STORE_PATH)
        if path is not None:
            self._path = path
        elif env_override:
            self._path = Path(env_override)
        else:
            self._path = _DEFAULT_STORE_PATH

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, ModelDispatchRecord]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "dispatch_history: failed to load %s: %s — starting empty",
                self._path,
                exc,
            )
            return {}

        records: dict[str, ModelDispatchRecord] = {}
        for ticket_id, payload in raw.items():
            try:
                records[ticket_id] = ModelDispatchRecord(**payload)
            except Exception as exc:
                logger.warning(
                    "dispatch_history: dropping malformed record for %s: %s",
                    ticket_id,
                    exc,
                )
        return records

    def save(self, records: dict[str, ModelDispatchRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        serializable = {
            ticket_id: json.loads(record.model_dump_json())
            for ticket_id, record in records.items()
        }
        fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=".dispatch-history-",
            suffix=".json",
        )
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(serializable, fh, indent=2, sort_keys=True)
            os.replace(tmp_path, self._path)
        except OSError:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def record_dispatch(
        self,
        ticket_id: str,
        *,
        correlation_id: str,
        now: datetime | None = None,
    ) -> ModelDispatchRecord:
        ts = now or datetime.now(tz=UTC)
        records = self.load()
        existing = records.get(ticket_id)
        if existing is None:
            updated = ModelDispatchRecord(
                ticket_id=ticket_id,
                first_dispatched_at=ts,
                last_dispatched_at=ts,
                attempt_count=1,
                last_correlation_id=correlation_id,
            )
        else:
            updated = ModelDispatchRecord(
                ticket_id=ticket_id,
                first_dispatched_at=existing.first_dispatched_at,
                last_dispatched_at=ts,
                attempt_count=existing.attempt_count + 1,
                last_correlation_id=correlation_id,
            )
        records[ticket_id] = updated
        self.save(records)
        return updated

    def clear_ticket(self, ticket_id: str) -> None:
        records = self.load()
        if ticket_id in records:
            del records[ticket_id]
            self.save(records)

    def is_stalled(
        self,
        ticket_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        records = self.load()
        record = records.get(ticket_id)
        if record is None:
            return False
        ts = now or datetime.now(tz=UTC)
        return (ts - record.last_dispatched_at) >= timedelta(
            minutes=STALL_WINDOW_MINUTES
        )

    def should_skip(self, ticket_id: str) -> bool:
        records = self.load()
        record = records.get(ticket_id)
        if record is None:
            return False
        return record.attempt_count >= MAX_DISPATCH_ATTEMPTS


__all__: list[str] = [
    "MAX_DISPATCH_ATTEMPTS",
    "STALL_WINDOW_MINUTES",
    "DispatchHistoryStore",
    "ModelDispatchRecord",
]
