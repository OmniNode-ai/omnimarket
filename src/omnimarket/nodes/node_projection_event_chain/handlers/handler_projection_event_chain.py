# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for the canonical ordered event-chain projection.

One canonical log entry in, one durable ``event_chain`` row out, ordered within a
correlation by a monotonic ``sequence``. The runtime auto-wiring projection path
(omnibase_infra.runtime.auto_wiring.handler_wiring._make_projection_dispatch_callback)
invokes ``handle(input_data)`` with a synchronous ``ProtocolProjectionDatabaseSync``
adapter injected at ``input_data['_db']`` and gates row materialization + the
projection terminal event on the returned ``rows_upserted`` count.

``handle()`` reads the adapter, builds the typed ``ModelEventChainProjectionEvent``
from the payload, and delegates to ``project()`` which:

* assigns the next ``sequence`` for the correlation (count of existing rows),
* dedups replays via the ``(correlation_id, envelope_id)`` conflict key — a
  replayed event keeps its original sequence and does not append a duplicate,
* upserts the ordered row.

Given a ``correlation_id``, the ordered chain reconstructs deterministically by
querying the rows and sorting on ``sequence`` (the read-side ``/projection/{topic}``
API does exactly this via its ``correlation_id`` filter + ``order_by``). This is
the canonical replacement for the bespoke SEA event-chain JSON ledger.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_projection_event_chain.models.model_event_chain_event import (
    ModelEventChainProjectionEvent,
)
from omnimarket.projection.protocol_database import ProtocolProjectionDatabaseSync

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
SUBSCRIBE_TOPICS = contract_subscribe_topics(_CONTRACT_PATH)
PUBLISH_TOPICS = contract_publish_topics(_CONTRACT_PATH)
SUBSCRIBE_TOPIC_LOG_ENTRY = SUBSCRIBE_TOPICS[0]
PUBLISH_TOPIC_CHAIN_APPLIED = PUBLISH_TOPICS[0]

TABLE = "event_chain"
# Composite conflict key — the runtime upsert splits on comma into
# ON CONFLICT (correlation_id, envelope_id).
CONFLICT_KEY = "correlation_id, envelope_id"


class ModelEventChainProjectionResult(BaseModel):
    """Result of an event-chain projection upsert."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionEventChain:
    """Materialize canonical events into ordered per-correlation chain rows."""

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim — materialize one canonical event.

        The runtime auto-wiring projection callback injects a
        ``ProtocolProjectionDatabaseSync`` adapter at ``input_data['_db']`` and a
        derived ``input_data['_event_type']`` string, then gates row
        materialization on the returned ``rows_upserted``. Build the typed event
        from the payload (envelope-stripped by the runtime), delegate to
        ``project()`` for the ordered upsert, and return the result mapping.
        """
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, ProtocolProjectionDatabaseSync):
            raise TypeError(
                "handle() requires a ProtocolProjectionDatabaseSync in "
                "input_data['_db']"
            )
        # _event_type is supplied by the runtime; this handler routes a single
        # log-entry source, so it is consumed (popped) but not branched on.
        payload.pop("_event_type", None)
        event = ModelEventChainProjectionEvent.model_validate(payload)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelEventChainProjectionEvent,
        db: ProtocolProjectionDatabaseSync,
    ) -> ModelEventChainProjectionResult:
        """Materialize one canonical event into the ordered chain.

        The ``sequence`` is the count of prior rows for the correlation — a
        monotonic per-correlation ordinal. A replayed event (same envelope_id)
        keeps its original sequence: the upsert conflict key
        ``(correlation_id, envelope_id)`` overwrites the same row rather than
        appending, so replay is idempotent and ordering is stable.
        """
        existing = db.query(TABLE, {"correlation_id": event.correlation_id})

        prior = next(
            (r for r in existing if r.get("envelope_id") == event.envelope_id),
            None,
        )
        sequence = (
            _int_value(prior.get("sequence")) if prior is not None else len(existing)
        )

        captured_at = _parse_datetime(event.timestamp).isoformat()
        causation_id = event.causation_id or event.correlation_id

        row: dict[str, object] = {
            "correlation_id": event.correlation_id,
            "sequence": sequence,
            "topic": event.topic,
            "source_node": event.source_node,
            "envelope_id": event.envelope_id,
            "causation_id": causation_id,
            "captured_at": captured_at,
            "payload": event.payload,
        }

        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelEventChainProjectionResult(rows_upserted=1 if ok else 0)


class NodeProjectionEventChain(HandlerProjectionEventChain):
    """ONEX entry-point wrapper for HandlerProjectionEventChain."""


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif value is not None:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.now(tz=UTC)
    else:
        dt = datetime.now(tz=UTC)
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


__all__ = [
    "PUBLISH_TOPIC_CHAIN_APPLIED",
    "SUBSCRIBE_TOPIC_LOG_ENTRY",
    "HandlerProjectionEventChain",
    "ModelEventChainProjectionResult",
    "NodeProjectionEventChain",
]
