# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerProjectionDelegationInferenceResponse — reducer for inference response text.

Consumes onex.evt.omnibase-infra.inference-response.v1 events carrying
ModelInferenceResponseData and materializes a singleton snapshot row into
projection_delegation_inference_response_text.

The snapshot exposes:
  - latest_* scalar fields from the most recent event
  - recent_responses: a FIFO rolling window (max MAX_HISTORY) of recent entries

Closes the NC-15 coverage gap: omnidash declares
onex.snapshot.projection.delegation.inference-response-text.v1 via the
DelegationModelOutputWidget (OMN-12745) but no reducer was emitting to it.

Ordering authority: Kafka topic/partition/offset (offsets tracked via _topic,
_partition, _offset keys injected by RuntimeLocal).
OMN-13088.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_projection_delegation_inference_response.models.model_inference_response_projection import (
    DEFAULT_TENANT,
    MAX_HISTORY,
    ModelInferenceResponseProjectionResult,
    ModelRecentInferenceResponse,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

_log = logging.getLogger(__name__)

TABLE = "projection_delegation_inference_response_text"
SOURCE_TOPIC = ""


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_recent(raw: object) -> list[dict[str, Any]]:
    """Deserialize recent_responses from either a JSON string or a Python list."""
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, str):
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    return []


class HandlerProjectionDelegationInferenceResponse:
    """Reduce inference-response events into a singleton snapshot row.

    Each call to ``handle()`` upserts the singleton row in
    ``projection_delegation_inference_response_text``, updating the
    ``latest_*`` scalar fields and prepending the new entry to the
    ``recent_responses`` JSONB window (capped at ``MAX_HISTORY``).
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

        # Fetch this tenant's current row to retrieve existing recent_responses.
        existing_rows = db.query(TABLE, filters={"singleton_key": tenant_id})
        recent: list[dict[str, Any]] = []
        if existing_rows:
            recent = _parse_recent(existing_rows[0].get("recent_responses", []))

        # Prepend the new entry and cap at MAX_HISTORY.
        updated_recent: list[dict[str, Any]] = [
            new_entry.model_dump(mode="json"),
            *recent,
        ][:MAX_HISTORY]

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
            "recent_responses": updated_recent,
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
