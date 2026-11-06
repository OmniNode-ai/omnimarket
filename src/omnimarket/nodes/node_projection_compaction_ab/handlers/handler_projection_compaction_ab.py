# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fold of one writer's event into a boundary row, in-memory store and SQL.

An event moves only its own writer's columns, and only when its
(event_time, event_id) is strictly newer than the one stored for that writer,
so redelivery and out-of-order delivery never move a row backwards.
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_compaction_ab.models import (
    EnumAbWriter,
    ModelAbEvent,
    ModelAbProjectionRequest,
    ModelAbProjectionResult,
    ModelAbRow,
    load_live_spec,
)
from omnimarket.nodes.node_projection_compaction_ab.models.model_compaction_ab import (
    EnumApplyReason,
)

TABLE = "omninode_internal.compaction_ab_rows"


def row_key(event: ModelAbEvent) -> tuple[str, str, str]:
    return (event.experiment_id, event.session_id, event.boundary_uuid)


class HandlerProjectionCompactionAb:
    def handle(self, request: ModelAbProjectionRequest) -> ModelAbProjectionResult:
        event = request.event
        prior = request.previous_row
        if prior is not None and (
            prior.experiment_id,
            prior.session_id,
            prior.boundary_uuid,
        ) != row_key(event):
            raise ValueError("previous row has a different key than the event")
        base = prior or ModelAbRow(
            experiment_id=event.experiment_id,
            session_id=event.session_id,
            boundary_uuid=event.boundary_uuid,
        )
        stored_time = getattr(base, f"{event.writer.value}_event_time")
        stored_id = getattr(base, f"{event.writer.value}_event_id")
        if stored_time is not None and (event.event_time, event.event_id) <= (
            stored_time,
            stored_id,
        ):
            return ModelAbProjectionResult(
                row=base, reason=EnumApplyReason.STALE_OR_DUPLICATE
            )
        update = {
            **event.values,
            f"{event.writer.value}_event_time": event.event_time,
            f"{event.writer.value}_event_id": event.event_id,
        }
        row = ModelAbRow.model_validate({**base.model_dump(), **update})
        return ModelAbProjectionResult(row=row, reason=EnumApplyReason.APPLIED)


class InMemoryAbStore:
    """Row-per-key store with the semantics the SQL upsert implements."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], ModelAbRow] = {}
        self._handler = HandlerProjectionCompactionAb()

    def apply(self, event: ModelAbEvent) -> ModelAbProjectionResult:
        key = row_key(event)
        result = self._handler.handle(
            ModelAbProjectionRequest(event=event, previous_row=self._rows.get(key))
        )
        self._rows[key] = result.row
        return result

    def rows(self) -> dict[tuple[str, str, str], ModelAbRow]:
        return dict(self._rows)


def upsert_sql(writer: EnumAbWriter) -> str:
    """The writer's upsert: its columns only, guarded by its own order columns."""
    spec = load_live_spec()
    keys = spec.key_columns
    owned = (
        *spec.writers[writer.value].columns,
        f"{writer.value}_event_time",
        f"{writer.value}_event_id",
    )
    columns = (*keys, *owned)
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in owned)
    order_time, order_id = owned[-2], owned[-1]
    return (
        f"INSERT INTO {TABLE} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {sets} "
        f"WHERE {TABLE}.{order_time} IS NULL "
        f"OR ({TABLE}.{order_time}, {TABLE}.{order_id}) < "
        f"(EXCLUDED.{order_time}, EXCLUDED.{order_id})"
    )


__all__ = ["HandlerProjectionCompactionAb", "InMemoryAbStore", "row_key", "upsert_sql"]
