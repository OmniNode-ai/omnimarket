# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's three tables over an injected DatabaseAdapter (in-memory by default, Postgres in the runtime)."""

from __future__ import annotations

from datetime import datetime

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelTicketContractWritten,
    ModelTicketHandEditFinding,
    ModelTicketWriteRecord,
)
from omnimarket.protocols.protocol_projection_database_sync import DatabaseAdapter

WRITE_RECORDS = "ticket_hand_edit_write_records"
CURSOR = "ticket_hand_edit_cursor"
OPEN_FINDINGS = "ticket_hand_edit_open_findings"
_CURSOR_KEY = "linear_issues"
_SEPARATOR = " | "


class TicketHandEditStore:
    """Raises whatever the adapter raises; the handler turns that into detector_unreadable."""

    def __init__(self, database: DatabaseAdapter) -> None:
        self._db = database

    def record_written(
        self, event: ModelTicketContractWritten, recorded_at: datetime
    ) -> bool:
        """Keep the newest written event per ticket; an older arrival never replaces it."""
        existing = self.newest_written(event.ticket_id)
        if (
            existing is not None
            and existing.linear_updated_at > event.linear_updated_at
        ):
            return False
        self._db.upsert(
            WRITE_RECORDS,
            "ticket_id",
            {
                "ticket_id": event.ticket_id,
                "contract_fingerprint": event.contract_fingerprint,
                "content_sha256": event.content_sha256,
                "linear_updated_at": event.linear_updated_at,
                "recorded_at": recorded_at,
            },
        )
        return True

    def newest_written(self, ticket_id: str) -> ModelTicketWriteRecord | None:
        rows = self._db.query(WRITE_RECORDS, {"ticket_id": ticket_id}, limit=1)
        if not rows:
            return None
        row = rows[0]
        return ModelTicketWriteRecord(
            ticket_id=str(row["ticket_id"]),
            contract_fingerprint=str(row["contract_fingerprint"] or ""),
            content_sha256=str(row["content_sha256"]),
            linear_updated_at=_as_datetime(row["linear_updated_at"]),
        )

    def cursor(self) -> datetime | None:
        rows = self._db.query(CURSOR, {"cursor_key": _CURSOR_KEY}, limit=1)
        return _as_datetime(rows[0]["updated_after"]) if rows else None

    def set_cursor(self, updated_after: datetime, recorded_at: datetime) -> None:
        self._db.upsert(
            CURSOR,
            "cursor_key",
            {
                "cursor_key": _CURSOR_KEY,
                "updated_after": updated_after,
                "recorded_at": recorded_at,
            },
        )

    def is_open(self, finding_key: str) -> bool:
        rows = self._db.query(OPEN_FINDINGS, {"finding_key": finding_key}, limit=1)
        return bool(rows) and rows[0]["closed_at"] is None

    def open_finding(
        self, finding: ModelTicketHandEditFinding, opened_at: datetime
    ) -> None:
        self._db.upsert(
            OPEN_FINDINGS,
            "finding_key",
            {
                "finding_key": finding.idempotency_key,
                "ticket_id": finding.ticket_id,
                "kind": finding.kind.value,
                "written_fingerprint": finding.written_fingerprint,
                "found_fingerprint": finding.found_fingerprint,
                "found_content_sha256": finding.found_content_sha256,
                "differences": _SEPARATOR.join(finding.differences),
                "editors": _SEPARATOR.join(finding.editors),
                "opened_at": opened_at,
                "closed_at": None,
                "closed_by": None,
            },
        )

    def open_findings_for(self, ticket_id: str) -> list[dict[str, object]]:
        return [
            row
            for row in self._db.query(OPEN_FINDINGS, {"ticket_id": ticket_id})
            if row["closed_at"] is None
        ]

    def close_finding(self, key: str, closed_at: datetime, closed_by: str) -> None:
        """Rewrite the whole row: an insert's NOT NULL columns are checked before the conflict resolves."""
        [*rows] = self._db.query(OPEN_FINDINGS, {"finding_key": key}, limit=1)
        if not rows:
            return
        self._db.upsert(
            OPEN_FINDINGS,
            "finding_key",
            {**rows[0], "closed_at": closed_at, "closed_by": closed_by},
        )


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


__all__ = [
    "CURSOR",
    "OPEN_FINDINGS",
    "WRITE_RECORDS",
    "TicketHandEditStore",
]
