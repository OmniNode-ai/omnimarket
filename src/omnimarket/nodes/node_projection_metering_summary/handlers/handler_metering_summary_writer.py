# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a writer: scope the Postgres pool to each runtime dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_metering_summary_refresh_requested import (
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.nodes.node_projection_metering_summary import (
    ModelMeteringSummaryFoldRequest,
    ModelMeteringSummaryRow,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_store_refresh import (
    ProtocolMeteringStoreReads,
    refresh_from_store,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_projection_metering_summary import (
    HandlerProjectionMeteringSummary,
)
from omnimarket.projection.protocol_database import ProtocolProjectionDatabaseSync
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta
from omnimarket.projection.tenant_isolation import resolve_write_tenant

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
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


@lru_cache(maxsize=1)
def store_refresh_command_topic() -> str:
    """The store refresh command topic, as the node's contract names it."""
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    topic = str(contract["config"]["metering_summary"]["store_refresh_command_topic"])
    if topic not in contract["event_bus"]["subscribe_topics"]:
        raise ValueError(f"{_CONTRACT_PATH} names {topic!r} but does not subscribe it")
    return topic


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
        path = contract_path or _CONTRACT_PATH
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

    @property
    def store_refresh_topic(self) -> str:
        """OMN-17427: the command that names tenants and days, not runs."""
        return store_refresh_command_topic()

    def _is_store_refresh(self, topic: object, data: dict[str, Any]) -> bool:
        if isinstance(topic, str) and topic:
            return topic == self.store_refresh_topic
        # An in-process dispatch without the envelope's topic: the two commands
        # share no required field, so the shape names the command.
        return "refresh_id" in data and "records" not in data

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        # The in-process dispatch hands the handler the envelope's own context
        # beside the payload, and the set it adds is not closed: on the dev lane
        # it also carried `_db`, `_event_type`, `_envelope_id` and
        # `_envelope_timestamp`, which both command models forbid, so every
        # scheduled refresh was validated into the DLQ and metering_summary
        # stayed empty while delegation_events grew (OMN-17427). Neither model
        # declares a field starting with an underscore -- pydantic cannot -- so
        # the prefix, not a name list, is what separates context from payload.
        data = {
            k: v
            for k, v in input_data.items()
            if k not in {"_topic", "_partition", "_offset", "_fallback_id"}
            and not k.startswith("_")
        }
        if self._is_store_refresh(input_data.get("_topic"), data):
            refresh = ModelMeteringSummaryRefreshRequested.model_validate(data)
            return asyncio.run(self._refresh_one_message(refresh))
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

    async def _refresh_one_message(
        self, request: ModelMeteringSummaryRefreshRequested
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            return await self._refresh(request)
        finally:
            await self.db.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        if self._is_store_refresh(topic, data):
            await self._refresh(
                ModelMeteringSummaryRefreshRequested.model_validate(data)
            )
            return True
        await self._project(ModelMeteringSummaryFoldRequest.model_validate(data))
        return True

    def _reads_for(self, table: str) -> ProtocolMeteringStoreReads:
        return self.db_for(table, operation="read")

    async def _refresh(
        self, request: ModelMeteringSummaryRefreshRequested
    ) -> dict[str, Any]:
        return await refresh_from_store(
            request, reads_for=self._reads_for, fold_and_store=self._project
        )

    async def _project(
        self, request: ModelMeteringSummaryFoldRequest
    ) -> dict[str, Any]:
        result = HandlerProjectionMeteringSummary().handle(request)
        count = 0
        for row in result.rows:
            values = row.model_dump(mode="json")
            # OMN-19513: the upsert names tenant_id, so the adapter refuses it
            # without the row's own tenant (OMN-15919). Unbound, every refresh
            # on the dev lane raised after folding, was redelivered, and, with
            # no `all` row ever stored, refolded every day of the tenant's runs
            # again: that loop held the main runtime's GIL ~70% of wall time
            # and starved every other projection on it, the hook-event
            # projection first (py-spy on the dev runtime, 2026-10-22T03:25Z).
            returned = await self.db_for("metering_summary").execute(
                _UPSERT,
                *(values[col] for col in _COLUMNS),
                tenant=resolve_write_tenant(
                    values["tenant_id"], table="metering_summary"
                ),
            )
            count += len(returned)
        return {
            "rows_upserted": count,
            "tenant_id": request.tenant_id,
            "baseline_model": request.baseline_model,
        }
