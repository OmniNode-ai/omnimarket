# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a effect writer for provider quota state (OMN-20154).

Persists the pure fold's row deltas with one atomic upsert per row. The merge
lives in SQL, not in a read-then-write, so two replicas projecting two calls on
the same key cannot lose a count between them.

Ordering: an observation not strictly newer than the row's ``observed_at`` is
refused. That makes replay idempotent (a redelivered event changes nothing)
at the cost of dropping an observation that arrives out of order, which
undercounts by one and, for a refusal, is re-learned on the next call.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_provider_quota.handlers.handler_projection_provider_quota import (
    HandlerProjectionProviderQuota,
)
from omnimarket.nodes.node_projection_provider_quota.models import (
    ModelProviderQuotaProjectionRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

# $1 tenant_id, $2 credential_ref, $3 provider_id, $4 model_scope,
# $5 observed_at, $6 window_seconds, $7 outcome, $8 http_status,
# $9 provider_code, $10 counts_hit, $11 sets_block, $12 disposition,
# $13 blocked_until, $14 blocked_indefinitely, $15 block_reason,
# $16 clears_blocks_before
_UPSERT_QUOTA_ROW = """
    INSERT INTO public.provider_quota_state AS t (
        tenant_id, credential_ref, provider_id, model_scope,
        calls_total, hits_total, window_seconds, window_started_at, window_calls,
        last_call_at, last_outcome, last_http_status, last_provider_code,
        last_hit_at, disposition, blocked_until, blocked_indefinitely,
        block_reason, observed_at, first_seen_at, updated_at
    )
    VALUES (
        $1, $2, $3, $4,
        1, CASE WHEN $10::boolean THEN 1 ELSE 0 END, $6, $5, 1,
        $5, $7, $8, $9,
        CASE WHEN $10::boolean THEN $5::timestamptz END,
        CASE WHEN $11::boolean THEN $12::text END,
        CASE WHEN $11::boolean THEN $13::timestamptz END,
        $11::boolean AND $14::boolean,
        CASE WHEN $11::boolean THEN $15::text END,
        $5, NOW(), NOW()
    )
    ON CONFLICT (tenant_id, credential_ref, provider_id, model_scope)
    DO UPDATE SET
        calls_total = t.calls_total + 1,
        hits_total = t.hits_total + CASE WHEN $10::boolean THEN 1 ELSE 0 END,
        window_seconds = EXCLUDED.window_seconds,
        window_started_at = CASE
            WHEN t.window_started_at IS NULL
              OR EXCLUDED.observed_at
                 >= t.window_started_at + make_interval(secs => EXCLUDED.window_seconds)
            THEN EXCLUDED.observed_at
            ELSE t.window_started_at
        END,
        window_calls = CASE
            WHEN t.window_started_at IS NULL
              OR EXCLUDED.observed_at
                 >= t.window_started_at + make_interval(secs => EXCLUDED.window_seconds)
            THEN 1
            ELSE t.window_calls + 1
        END,
        last_call_at = EXCLUDED.observed_at,
        last_outcome = EXCLUDED.last_outcome,
        last_http_status = EXCLUDED.last_http_status,
        last_provider_code = EXCLUDED.last_provider_code,
        last_hit_at = CASE WHEN $10::boolean THEN EXCLUDED.observed_at ELSE t.last_hit_at END,
        disposition = CASE
            WHEN $11::boolean THEN EXCLUDED.disposition
            WHEN $16::timestamptz IS NOT NULL
             AND (t.last_hit_at IS NULL OR t.last_hit_at < $16::timestamptz) THEN NULL
            ELSE t.disposition
        END,
        blocked_until = CASE
            WHEN $11::boolean THEN GREATEST(t.blocked_until, EXCLUDED.blocked_until)
            WHEN $16::timestamptz IS NOT NULL
             AND (t.last_hit_at IS NULL OR t.last_hit_at < $16::timestamptz) THEN NULL
            ELSE t.blocked_until
        END,
        blocked_indefinitely = CASE
            WHEN $11::boolean THEN t.blocked_indefinitely OR EXCLUDED.blocked_indefinitely
            WHEN $16::timestamptz IS NOT NULL
             AND (t.last_hit_at IS NULL OR t.last_hit_at < $16::timestamptz) THEN FALSE
            ELSE t.blocked_indefinitely
        END,
        block_reason = CASE
            WHEN $11::boolean THEN EXCLUDED.block_reason
            WHEN $16::timestamptz IS NOT NULL
             AND (t.last_hit_at IS NULL OR t.last_hit_at < $16::timestamptz) THEN NULL
            ELSE t.block_reason
        END,
        observed_at = EXCLUDED.observed_at,
        updated_at = NOW()
    WHERE t.observed_at < EXCLUDED.observed_at
    RETURNING projection_cursor
"""


class ProviderQuotaProjectionWriter(BaseProjectionRunner):
    """One event loop and pool per in-process dispatch, explicit tenant context."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        self._contract: dict[str, Any] = yaml.safe_load(path.read_text())
        self._derive = HandlerProjectionProviderQuota()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["subscribe_topics"])

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        data = dict(input_data)
        topic = str(data.pop("_topic", self.subscribe_topics[0]))
        meta = MessageMeta(
            partition=int(data.pop("_partition", 0)),
            offset=int(data.pop("_offset", 0)),
            fallback_id=str(data.pop("_fallback_id", "")),
            topic=topic,
        )
        return asyncio.run(self._project_one_message(data, meta))

    async def _project_one_message(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            written = await self._project_event(data, meta)
        finally:
            await self.db.close()
        return {"rows_upserted": len(written), "quota_rows": written}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_event(data, meta)
        return True

    async def _project_event(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> list[dict[str, Any]]:
        result = self._derive.handle(
            ModelProviderQuotaProjectionRequest.model_validate(data)
        )
        written: list[dict[str, Any]] = []
        for row in result.rows:
            returned = await self.db.execute(
                _UPSERT_QUOTA_ROW,
                row.tenant_id,
                row.credential_ref,
                row.provider_id,
                row.model_scope,
                row.observed_at,
                row.window_seconds,
                row.outcome.value,
                row.http_status,
                row.provider_code,
                row.counts_hit,
                row.sets_block,
                row.disposition,
                row.blocked_until,
                row.blocked_indefinitely,
                row.block_reason,
                row.clears_blocks_before,
                tenant=str(row.tenant_id),
            )
            if returned:
                written.append(
                    {
                        "tenant_id": str(row.tenant_id),
                        "credential_ref": row.credential_ref,
                        "provider_id": row.provider_id,
                        "model_scope": row.model_scope,
                        **dict(returned[0]),
                    }
                )
        return written
