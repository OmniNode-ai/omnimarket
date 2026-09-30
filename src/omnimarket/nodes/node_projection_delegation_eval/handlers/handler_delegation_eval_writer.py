# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a effect writer for delegation evaluation labels.

Prompt/response content lives on the lab table only because omnimarket is
public. Applied notifications contain row identities, never snapshot content.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_delegation_eval.handlers.handler_projection_delegation_eval import (
    HandlerProjectionDelegationEval,
)
from omnimarket.nodes.node_projection_delegation_eval.models import (
    ModelDelegationEvalProjectionRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

_UPSERT_ITEM = """
    INSERT INTO public.delegation_eval_items (
        tenant_id, item_key, correlation_id, attempt_index, task_class, stratum,
        prompt_snapshot, response_snapshot, gate_verdict, deciding_check,
        label, rater_role, rubric_version, computed_facts, observed_at,
        first_seen_at, updated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb, $15, NOW(), NOW())
    ON CONFLICT (tenant_id, item_key, rater_role, rubric_version)
    DO UPDATE SET
        correlation_id = EXCLUDED.correlation_id,
        attempt_index = EXCLUDED.attempt_index,
        task_class = EXCLUDED.task_class,
        stratum = EXCLUDED.stratum,
        prompt_snapshot = EXCLUDED.prompt_snapshot,
        response_snapshot = EXCLUDED.response_snapshot,
        gate_verdict = EXCLUDED.gate_verdict,
        deciding_check = EXCLUDED.deciding_check,
        label = EXCLUDED.label,
        computed_facts = EXCLUDED.computed_facts,
        observed_at = EXCLUDED.observed_at,
        updated_at = NOW()
    WHERE public.delegation_eval_items.observed_at < EXCLUDED.observed_at
    RETURNING projection_cursor
"""


class DelegationEvalProjectionWriter(BaseProjectionRunner):
    """One event loop and pool per in-process dispatch, explicit tenant context."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        self._contract: dict[str, Any] = yaml.safe_load(path.read_text())
        self._derive = HandlerProjectionDelegationEval()

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
        return {"rows_upserted": len(written), "item_rows": written}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_event(data, meta)
        return True

    async def _project_event(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> list[dict[str, Any]]:
        result = self._derive.handle(
            ModelDelegationEvalProjectionRequest.model_validate(data)
        )
        written: list[dict[str, Any]] = []
        for row in result.rows:
            returned = await self.db.execute(
                _UPSERT_ITEM,
                row.tenant_id,
                row.item_key,
                row.correlation_id,
                row.attempt_index,
                row.task_class,
                row.stratum,
                row.prompt_snapshot,
                row.response_snapshot,
                row.gate_verdict,
                row.deciding_check,
                row.label,
                row.rater_role,
                row.rubric_version,
                json.dumps(row.computed_facts),
                row.observed_at,
                tenant=str(row.tenant_id),
            )
            if returned:
                written.append(
                    {
                        "tenant_id": str(row.tenant_id),
                        "item_key": row.item_key,
                        "rater_role": row.rater_role,
                        "rubric_version": row.rubric_version,
                        **dict(returned[0]),
                    }
                )
        return written
