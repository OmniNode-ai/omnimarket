# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tenant-scoped effect writer with deterministic replacement (OMN-20242)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_delegation_disposition.handlers.handler_projection_delegation_disposition import (
    HandlerProjectionDelegationDisposition,
)
from omnimarket.nodes.node_projection_delegation_disposition.models import (
    ModelDelegationDispositionProjectionRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

_UPSERT_DISPOSITION = """
    INSERT INTO public.delegation_dispositions (
        tenant_id, delegation_correlation_id, disposition, reason_code, caller_lane,
        engine, artifact_kind, artifact_ref, edit_ratio, ticket_id, answer_sha256,
        recorded_at, disposition_id
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
    ON CONFLICT (tenant_id, delegation_correlation_id)
    DO UPDATE SET
        disposition_id = EXCLUDED.disposition_id,
        disposition = EXCLUDED.disposition,
        reason_code = EXCLUDED.reason_code,
        caller_lane = EXCLUDED.caller_lane,
        engine = EXCLUDED.engine,
        artifact_kind = EXCLUDED.artifact_kind,
        artifact_ref = EXCLUDED.artifact_ref,
        edit_ratio = EXCLUDED.edit_ratio,
        ticket_id = EXCLUDED.ticket_id,
        answer_sha256 = EXCLUDED.answer_sha256,
        recorded_at = EXCLUDED.recorded_at
    WHERE (public.delegation_dispositions.recorded_at, public.delegation_dispositions.disposition_id)
        < (EXCLUDED.recorded_at, EXCLUDED.disposition_id)
    RETURNING disposition_id
"""


class HandlerDelegationDispositionWriter(BaseProjectionRunner):
    """One loop and pool per dispatch; duplicates count as handled rows."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        self._contract: dict[str, Any] = yaml.safe_load(path.read_text())
        self._derive = HandlerProjectionDelegationDisposition()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["subscribe_topics"])

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        data = dict(input_data)
        # The kernel injects its synchronous adapter. This runner owns the
        # tenant-scoped async pool, connected inside the dispatch loop below.
        data.pop("_db", None)
        meta = MessageMeta(
            topic=str(data.pop("_topic", self.subscribe_topics[0])),
            partition=int(data.pop("_partition", 0)),
            offset=int(data.pop("_offset", 0)),
            fallback_id=str(data.pop("_fallback_id", "")),
        )
        return asyncio.run(self._project_one_message(data, meta))

    async def _project_one_message(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            return await self._project_event(data, meta)
        finally:
            await self.db.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_event(data, meta)
        return True

    async def _project_event(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        result = self._derive.handle(
            ModelDelegationDispositionProjectionRequest.model_validate(data)
        )
        upserted = refused = 0
        identities: list[dict[str, str]] = []
        for row in result.rows:
            returned = await self.db.execute(
                _UPSERT_DISPOSITION,
                row.tenant_id,
                row.delegation_correlation_id,
                row.disposition,
                row.reason_code,
                row.caller_lane,
                row.engine,
                row.artifact_kind,
                row.artifact_ref,
                row.edit_ratio,
                row.ticket_id,
                row.answer_sha256,
                row.recorded_at,
                row.disposition_id,
                tenant=str(row.tenant_id),
            )
            upserted += int(bool(returned))
            refused += int(not returned)
            identities.append(
                {
                    "tenant_id": str(row.tenant_id),
                    "delegation_correlation_id": str(row.delegation_correlation_id),
                    "disposition_id": str(row.disposition_id),
                }
            )
        return {
            "rows_handled": upserted + refused,
            "rows_upserted": upserted,
            "rows_refused_by_ordering_guard": refused,
            "disposition_rows": identities,
        }
