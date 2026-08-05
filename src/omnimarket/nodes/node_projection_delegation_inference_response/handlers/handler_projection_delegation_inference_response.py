# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerProjectionDelegationInferenceResponse — reducer for inference response text.

Consumes onex.evt.omnibase-infra.inference-response.v1 events carrying
ModelInferenceResponseData and materializes a singleton snapshot row into
projection_delegation_inference_response_text.

The snapshot exposes:
  - latest_* scalar fields from the most recent event
  - recent_responses: the newest entry only (see OMN-15707 note below)

Closes the NC-15 coverage gap: omnidash declares
onex.snapshot.projection.delegation.inference-response-text.v1 via the
DelegationModelOutputWidget (OMN-12745) but no reducer was emitting to it.

Ordering authority: Kafka topic/partition/offset (offsets tracked via _topic,
_partition, _offset keys injected by RuntimeLocal).
OMN-13088.

OMN-15707: ``project()`` previously called ``db.query()`` before every
upsert to read back the tenant's existing ``recent_responses`` row so the
new entry could be prepended and capped at ``MAX_HISTORY`` (a true FIFO
rolling window). The contract (``contract.yaml``) declares ``access: write``
only for this table, and the real runtime enforces that per-call
(``ProjectionTableOperation._assert_read_declared``,
``omnibase_infra/runtime/auto_wiring/handler_wiring.py:2252-2257``) -- every
live Postgres-backed dispatch raised ``PermissionError`` and DLQ'd the event
(correlation ce0bff7a-95e7-4656-8bcc-98a021f125ea, run 30970742725; 100% of
events for this table failed, so the window was never actually populated
past the migration's empty-array seed row).

Unlike the sibling fix in node_projection_live_events (OMN-15705,
omnimarket#2016), the pre-read here cannot be dropped by relying on a SQL
column DEFAULT plus SET-clause omission: ``recent_responses`` is a *function
of its own prior value* (prepend + cap), not a value that should simply be
left untouched on conflict. Neither the sync adapters in this repo
(``postgres_sync_database.py``, ``sqlite_database.py``) nor the production
``ProjectionDatabaseOperations._execute_upsert`` in omnibase_infra support a
computed/expression SET value (both build
``"{column}" = EXCLUDED."{column}"`` from literal parameters only) -- doing
this correctly server-side (e.g. a Postgres ``jsonb`` concat-and-trim
expression against the existing column) would require adding that
capability across both this repo and omnibase_infra, out of scope for this
live-firing-DLQ fix.

Disclosed behavior change: ``recent_responses`` now always contains just the
current event's entry (window size 1) instead of a growing rolling window,
until a follow-up restores true FIFO history via an infra-side computed
upsert expression. This is a net improvement over the current live state
(100% DLQ, window never populated at all) and does not touch the contract
declaration or ``_assert_read_declared``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_projection_delegation_inference_response.models.model_inference_response_projection import (
    DEFAULT_TENANT,
    ModelInferenceResponseProjectionResult,
    ModelRecentInferenceResponse,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

_log = logging.getLogger(__name__)

TABLE = "projection_delegation_inference_response_text"
SOURCE_TOPIC = ""


def _now_utc() -> datetime:
    return datetime.now(UTC)


class HandlerProjectionDelegationInferenceResponse:
    """Reduce inference-response events into a singleton snapshot row.

    Each call to ``handle()`` upserts the singleton row in
    ``projection_delegation_inference_response_text``, updating the
    ``latest_*`` scalar fields and ``recent_responses`` to a single-entry
    array holding just this event (see OMN-15707 module docstring note).
    """

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler shim.

        Expected extra keys injected by the runtime:
          _db         : DatabaseAdapter
          _topic      : str  (source Kafka topic)
          _partition  : int
          _offset     : int

        All remaining keys are forwarded as the event payload.
        """
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")

        topic = str(payload.pop("_topic", SOURCE_TOPIC))
        payload.pop("_partition", None)
        payload.pop("_offset", None)

        result = self.project(payload, db_raw, topic=topic)
        return result.model_dump(mode="json")

    def project(
        self,
        payload: dict[str, Any],
        db: DatabaseAdapter,
        *,
        topic: str = SOURCE_TOPIC,
    ) -> ModelInferenceResponseProjectionResult:
        """Upsert the per-tenant snapshot row from a raw event payload dict.

        OMN-14894 (tranche 2): this table was a single global singleton
        (singleton_key always 'global') -- every tenant's inference-response
        event overwrote the same row, a confirmed active cross-tenant leak
        (Linear OMN-14894 comment 6b84daf0). ModelInferenceResponseData
        carries tenant_id (OMN-14280) but this handler previously dropped it
        entirely. It now reads tenant_id from the payload (falling back to
        DEFAULT_TENANT when absent, never silently omitted) and re-keys the
        upsert conflict key to the resolved tenant identity, so each tenant
        gets its own row instead of colliding on 'global'.
        """
        now = _now_utc()

        correlation_id = str(payload.get("correlation_id", ""))
        model_used = str(payload.get("model_used", ""))
        task_type = str(payload.get("task_type", ""))
        content = str(payload.get("content", ""))
        prompt_tokens = int(payload.get("prompt_tokens", 0))
        completion_tokens = int(payload.get("completion_tokens", 0))
        latency_ms = int(payload.get("latency_ms", 0))

        raw_tenant_id = payload.get("tenant_id")
        tenant_id = (
            str(raw_tenant_id)
            if isinstance(raw_tenant_id, str) and raw_tenant_id.strip()
            else DEFAULT_TENANT
        )

        # Build the new recent-response entry.
        new_entry = ModelRecentInferenceResponse(
            correlation_id=correlation_id,
            model_name=model_used,
            task_type=task_type,
            generated_text=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            captured_at=now,
        )

        # OMN-15707: no read-back. recent_responses is the current event
        # only (window size 1) -- see module docstring for why the prior
        # read-then-prepend-then-cap approach cannot be preserved without a
        # read against this write-only-declared table.
        row: dict[str, object] = {
            "singleton_key": tenant_id,
            "tenant_id": tenant_id,
            "latest_correlation_id": correlation_id,
            "latest_model_name": model_used,
            "latest_task_type": task_type,
            "latest_generated_text": content,
            "latest_prompt_tokens": prompt_tokens,
            "latest_completion_tokens": completion_tokens,
            "latest_latency_ms": latency_ms,
            "source_topic": topic,
            "recent_responses": [new_entry.model_dump(mode="json")],
            "captured_at": now.isoformat(),
            "provisioned": True,
        }

        db.upsert(TABLE, "singleton_key", row)
        _log.debug(
            "projection_delegation_inference_response: upserted singleton",
            extra={"correlation_id": correlation_id, "model": model_used},
        )

        return ModelInferenceResponseProjectionResult(
            rows_upserted=1, singleton_key=tenant_id
        )
