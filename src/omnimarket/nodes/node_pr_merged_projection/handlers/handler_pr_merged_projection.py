# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrMergedProjection — project pr-merged events to the projection store.

Consumes ``onex.evt.github.pr-merged.v1`` (published by the pr-merged-publisher
GHA workflow on every repo merge, OMN-13226 / T2) and UPSERTs one row per event
into the ``pr_merged_events`` table, deduped by ``event_id``. Idempotent:
projecting the same event twice leaves exactly one row.

The generic projection API serves the materialized rows at
``GET /projection/onex.evt.github.pr-merged.v1?since=<cursor>`` on the local
``:3002`` lane. The per-machine worktree reaper (OMN-13228 / T4) polls that
endpoint, matches ``{repo, branch, pr_number, ticket}`` to a local worktree, and
runs ``prune-worktrees.sh`` against it.

The reducer performs no broker, no URL, and no filesystem I/O — it folds one
event into one projection row through the injected database adapter.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.github import ModelPrMergedEvent
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "pr_merged_events"
CONFLICT_KEY = "event_id"


class ModelProjectionResult(BaseModel):
    """Outcome of projecting one pr-merged event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerPrMergedProjection:
    """Project ``onex.evt.github.pr-merged.v1`` events into ``pr_merged_events``."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """Runtime dispatch shim.

        The projection host delivers the event payload plus a ``DatabaseAdapter``
        under ``input_data['_db']``. Coerce the payload into a
        ``ModelPrMergedEvent`` and project it. Fail fast when the adapter is
        absent — a missing adapter is a wiring bug, not a recoverable state.
        """
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        payload.pop("_event_type", None)
        event = ModelPrMergedEvent.model_validate(payload)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelPrMergedEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a single pr-merged event, deduped by ``event_id``.

        ``projection_cursor`` is a DB-assigned monotonic BIGSERIAL and is never
        written by the handler, so the cursor advances strictly on insert.
        """
        row: dict[str, object] = {
            "event_id": event.event_id,
            "repo": event.repo,
            "branch": event.branch,
            "pr_number": event.pr_number,
            "ticket": event.ticket,
            "merged_at": event.merged_at,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)


__all__: list[str] = [
    "HandlerPrMergedProjection",
    "ModelProjectionResult",
]
