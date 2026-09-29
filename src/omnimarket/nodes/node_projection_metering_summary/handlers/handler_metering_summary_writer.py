# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a writer: scope the Postgres pool to each runtime dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_metering_summary import (
    ModelMeteringSummaryFoldRequest,
    ModelMeteringSummaryRow,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
)
from omnimarket.projection.protocol_database import ProtocolProjectionDatabaseSync
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

CONFLICT_KEY = "tenant_id,window_kind,window_start,baseline_model"
_COLUMNS = tuple(ModelMeteringSummaryRow.model_fields)
_UPSERT = (
    f"INSERT INTO public.metering_summary ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join(f'${i}' for i in range(1, len(_COLUMNS) + 1))}) "
    f"ON CONFLICT ({CONFLICT_KEY}) DO UPDATE SET "
    + ", ".join(
        f"{col} = EXCLUDED.{col}"
        for col in _COLUMNS
        if col not in CONFLICT_KEY.split(",")
    )
    + " RETURNING tenant_id"
)


def store_rows(
    db: ProtocolProjectionDatabaseSync, rows: Sequence[ModelMeteringSummaryRow]
) -> None:
    """Replace snapshots through the existing store-neutral upsert boundary."""
    for row in rows:
        db.upsert("metering_summary", CONFLICT_KEY, row.model_dump(mode="json"))


class MeteringSummaryProjectionWriter(BaseProjectionRunner):
    """Accept a full snapshot request, fold it, and replace its stored rows."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        self._contract: dict[str, Any] = yaml.safe_load(path.read_text())

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["subscribe_topics"])

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["dlq_topics"])

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        data = {
            k: v
            for k, v in input_data.items()
            if k not in {"_topic", "_partition", "_offset", "_fallback_id"}
        }
        request = ModelMeteringSummaryFoldRequest.model_validate(data)
        return asyncio.run(self._project_one_message(request))

    async def _project_one_message(
        self, request: ModelMeteringSummaryFoldRequest
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            return await self._project(request)
        finally:
            await self.db.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project(ModelMeteringSummaryFoldRequest.model_validate(data))
        return True

    async def _project(
        self, request: ModelMeteringSummaryFoldRequest
    ) -> dict[str, Any]:
        result = HandlerProjectionMeteringSummary().handle(request)
        count = 0
        for row in result.rows:
            values = row.model_dump(mode="json")
            returned = await self.db_for("metering_summary").execute(
                _UPSERT, *(values[col] for col in _COLUMNS)
            )
            count += len(returned)
        return {
            "rows_upserted": count,
            "tenant_id": request.tenant_id,
            "baseline_model": request.baseline_model,
        }
