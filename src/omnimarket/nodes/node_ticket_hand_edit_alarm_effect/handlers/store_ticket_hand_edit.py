# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's three tables over an injected DatabaseAdapter (in-memory in tests, Postgres in the runtime)."""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.models.model_ticket_contract_written import ModelTicketContractWritten
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelTicketHandEditFinding,
)
from omnimarket.protocols.protocol_projection_database_sync import DatabaseAdapter

WRITTEN = "ticket_hand_edit_written"
CURSOR = "ticket_hand_edit_cursor"
OPEN_FINDINGS = "ticket_hand_edit_open_findings"
_CURSOR_NAME = "linear_issues_updated_at"
OPEN = "open"
CLOSED = "closed"


class TicketHandEditStore:
    """Raises whatever the adapter raises; the handler turns that into detector_unreadable."""

    def __init__(self, database: DatabaseAdapter) -> None:
        self._db = database

    def record_written(self, event: ModelTicketContractWritten) -> bool:
        """Keep the newest written event per ticket; the newer Linear updatedAt wins."""
        [*existing] = self._db.query(WRITTEN, {"ticket_id": event.ticket_id}, limit=1)
        if existing and _as_datetime(existing[0]["updated_at"]) > event.updated_at:
            return False
        self._db.upsert(
            WRITTEN,
            "ticket_id",
            {
                "ticket_id": event.ticket_id,
                "contract_fingerprint": event.contract_fingerprint,
                "content_sha256": event.content_sha256,
                "updated_at": event.updated_at,
                "kind": event.kind,
                "correlation_id": str(event.correlation_id or ""),
            },
        )
        return True

    def newest_written(self, ticket_id: str) -> ModelTicketContractWritten | None:
        rows = self._db.query(WRITTEN, {"ticket_id": ticket_id}, limit=1)
        if not rows:
            return None
        row = rows[0]
        return ModelTicketContractWritten(
            ticket_id=str(row["ticket_id"]),
            contract_fingerprint=str(row["contract_fingerprint"] or ""),
            content_sha256=str(row["content_sha256"]),
            updated_at=_as_datetime(row["updated_at"]),
            kind="adopted" if row.get("kind") == "adopted" else "written",
        )

    def cursor(self) -> datetime | None:
        rows = self._db.query(CURSOR, {"name": _CURSOR_NAME}, limit=1)
        return _as_datetime(rows[0]["position"]) if rows else None

    def move_cursor(self, position: datetime) -> None:
        self._db.upsert(CURSOR, "name", {"name": _CURSOR_NAME, "position": position})

    def is_open(self, idempotency_key: str) -> bool:
        rows = self._db.query(
            OPEN_FINDINGS, {"idempotency_key": idempotency_key}, limit=1
        )
        return bool(rows) and rows[0].get("status") == OPEN

    def open_finding(self, finding: ModelTicketHandEditFinding) -> None:
        self._db.upsert(
            OPEN_FINDINGS,
            "idempotency_key",
            {
                "idempotency_key": finding.idempotency_key,
                "kind": finding.kind.value,
                "ticket_id": finding.ticket_id or "",
                "fingerprint_written": finding.fingerprint_written or "",
                "fingerprint_found": finding.fingerprint_found or "",
                "content_sha256_found": finding.content_sha256_found or "",
                "differs": "; ".join(finding.differs),
                "editors": ",".join(finding.editors),
                "reason": finding.reason or "",
                "status": OPEN,
                "opened_at": finding.detected_at,
                "closed_at": None,
            },
        )

    def close_matching(
        self, event: ModelTicketContractWritten, now: datetime
    ) -> list[str]:
        """Close the ticket's open ticket_hand_edit findings whose found content the write matches."""
        closed: list[str] = []
        for row in self._db.query(OPEN_FINDINGS, {"ticket_id": event.ticket_id}):
            if row.get("status") != OPEN:
                continue
            if row.get("content_sha256_found") != event.content_sha256:
                continue
            self._db.upsert(
                OPEN_FINDINGS,
                "idempotency_key",
                {**row, "status": CLOSED, "closed_at": now},
            )
            closed.append(str(row["idempotency_key"]))
        return closed


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value))


__all__ = ["CURSOR", "OPEN_FINDINGS", "WRITTEN", "TicketHandEditStore"]
