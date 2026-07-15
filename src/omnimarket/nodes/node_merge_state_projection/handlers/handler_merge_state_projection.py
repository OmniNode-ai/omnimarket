# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerMergeStateProjection — project merge-state transitions (OMN-14648 / WS6).

Consumes ``onex.evt.omnimarket.merge-state-transition.v1`` and UPSERTs one row
per transition into the ``merge_state_transitions`` table, deduped by the
deterministic ``event_id`` fingerprint. Idempotent: projecting the same
transition twice leaves exactly one row, so a replay of the merge-flow event log
rebuilds an identical projection.

The reducer performs no broker, no URL, and no filesystem I/O — it folds one
event into one projection row through the injected database adapter. The
downstream merge-flow metrics (``merge_state_metrics_native``) are materialized
from the rows this projector writes; that measurement gates any future
auto-merge decision (REPORT-ONLY for now — no enforcement in this node).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.merge_state import ModelMergeStateTransitionEvent
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "merge_state_transitions"
CONFLICT_KEY = "event_id"


class ModelMergeStateProjectionResult(BaseModel):
    """Outcome of projecting one merge-state transition event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerMergeStateProjection:
    """Project ``merge-state-transition.v1`` events into ``merge_state_transitions``."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """Runtime dispatch shim.

        The projection host delivers the event payload plus a ``DatabaseAdapter``
        under ``input_data['_db']``. Coerce the payload into a
        ``ModelMergeStateTransitionEvent`` and project it. Fail fast when the
        adapter is absent — a missing adapter is a wiring bug, not a recoverable
        state.
        """
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        payload.pop("_event_type", None)
        # ``event_id`` is a computed field; drop any inbound value so the model
        # recomputes the deterministic fingerprint from the identifying tuple.
        payload.pop("event_id", None)
        event = ModelMergeStateTransitionEvent.model_validate(payload)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelMergeStateTransitionEvent,
        db: DatabaseAdapter,
    ) -> ModelMergeStateProjectionResult:
        """UPSERT a single merge-state transition, deduped by ``event_id``.

        ``projection_cursor`` is a DB-assigned monotonic BIGSERIAL and is never
        written by the handler, so the cursor advances strictly on insert.
        """
        row: dict[str, object] = {
            "event_id": event.event_id,
            "repo": event.repo,
            "pr_number": event.pr_number,
            "head_sha": event.head_sha,
            "branch": event.branch,
            "from_state": event.from_state.value,
            "to_state": event.to_state.value,
            "occurred_at": event.occurred_at,
            "reason_code": event.reason_code.value if event.reason_code else None,
            "is_occ_evidence": event.is_occ_evidence,
            "product_pr_number": event.product_pr_number,
            "queue_wait_seconds": event.queue_wait_seconds,
            "product_failure_found": event.product_failure_found,
            "evidence_present": event.evidence_present,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelMergeStateProjectionResult(rows_upserted=1 if ok else 0)


__all__ = [
    "HandlerMergeStateProjection",
    "ModelMergeStateProjectionResult",
]
