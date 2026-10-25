# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a effect writer of the delegation capability map (OMN-20169).

A map-run event is folded in process by the pure capability map compute and
each cell is upserted into ``delegation_capability_cells`` under the run's
tenant, keyed by map run, model, backend, configuration, class and band. A
redelivered run carries the same ``observed_at`` and writes no row; that row is
counted as refused by the ordering guard, so a non-empty run never reports
zero rows handled.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.delegation_capability_map.model_capability_map_run_recorded import (
    ModelCapabilityMapRunRecorded,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers.handler_projection_delegation_capability_map import (
    HandlerProjectionDelegationCapabilityMap,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

_UPSERT_CELL = """
    INSERT INTO public.delegation_capability_cells (
        tenant_id, map_run_id, model_key, backend_id, configuration_id,
        task_class, size_band, configuration_parameters, n, passes, pass_rate,
        wilson_low, wilson_high, decision_lower_bound, floor, required_n,
        verdict, observed_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12, $13, $14,
            $15, $16, $17, $18)
    ON CONFLICT (tenant_id, map_run_id, model_key, backend_id, configuration_id,
                 task_class, size_band)
    DO UPDATE SET
        configuration_parameters = EXCLUDED.configuration_parameters,
        n = EXCLUDED.n,
        passes = EXCLUDED.passes,
        pass_rate = EXCLUDED.pass_rate,
        wilson_low = EXCLUDED.wilson_low,
        wilson_high = EXCLUDED.wilson_high,
        decision_lower_bound = EXCLUDED.decision_lower_bound,
        floor = EXCLUDED.floor,
        required_n = EXCLUDED.required_n,
        verdict = EXCLUDED.verdict,
        observed_at = EXCLUDED.observed_at
    WHERE public.delegation_capability_cells.observed_at < EXCLUDED.observed_at
    RETURNING map_run_id
"""


class HandlerDelegationCapabilityMapWriter(BaseProjectionRunner):
    """One loop and pool per dispatch; a declined row counts as handled."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        self._contract: dict[str, Any] = yaml.safe_load(path.read_text())
        self._derive = HandlerProjectionDelegationCapabilityMap()

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
        del meta
        run = ModelCapabilityMapRunRecorded.model_validate(data)
        result = self._derive.handle(run)
        upserted = refused = 0
        for cell in result.cells:
            returned = await self.db.execute(
                _UPSERT_CELL,
                cell.tenant_id,
                cell.map_run_id,
                cell.model_key,
                cell.backend_id,
                cell.configuration_id,
                cell.task_class,
                cell.size_band.value,
                cell.configuration_parameters.model_dump_json(),
                cell.n,
                cell.passes,
                cell.pass_rate,
                cell.wilson_low,
                cell.wilson_high,
                cell.decision_lower_bound,
                cell.floor,
                cell.required_n,
                cell.verdict.value,
                run.observed_at,
                tenant=str(cell.tenant_id),
            )
            upserted += int(bool(returned))
            refused += int(not returned)
        return {
            "rows_handled": upserted + refused,
            "rows_upserted": upserted,
            "rows_refused_by_ordering_guard": refused,
            "map_run_id": run.map_run_id,
            "cells": len(result.cells),
            "folded_rows": result.folded_row_count,
            "inadmissible": {
                entry.reason.value: entry.count for entry in result.inadmissible
            },
        }
