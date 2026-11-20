# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a effect writer for recorded experiment-arm results.

One row per scored arm in ``experiment_arm_results``, keyed by
(tenant_id, experiment_id, arm, model_id, measured_at). A new measured_at for
the same arm is a NEW row, so accuracy over time keeps its history; a
redelivered event conflicts, changes nothing and is counted in
``rows_refused_by_ordering_guard``. Applied notifications carry row
identities, never snapshot content; the snapshot itself goes out as one keyed
delta per new row on the bus-backed evals exposure.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_experiment_result.handlers.handler_projection_experiment_result import (
    HandlerProjectionExperimentResult,
)
from omnimarket.nodes.node_projection_experiment_result.models import (
    ModelExperimentResultProjectionRequest,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
)

_UPSERT = """
    INSERT INTO public.experiment_arm_results (
        tenant_id, experiment_id, arm, model_id, engine, n, correct, accuracy,
        wilson_low, wilson_high, baseline_accuracy, advantage_over_baseline,
        clears_baseline, latency_p50_ms, latency_p90_ms, gpu_seconds,
        corpus_sha256, truth_sha256, report_ref, measured_at,
        first_seen_at, updated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
            $16, $17, $18, $19, $20, NOW(), NOW())
    ON CONFLICT (tenant_id, experiment_id, arm, model_id, measured_at)
    DO NOTHING
    RETURNING projection_cursor
"""


class ExperimentResultProjectionWriter(BaseProjectionRunner):
    """One event loop and pool per in-process dispatch, explicit tenant context."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        self._contract: dict[str, Any] = yaml.safe_load(path.read_text())
        self._derive = HandlerProjectionExperimentResult()
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (
                exposure
                for exposure in load_projection_exposures_from_contract(
                    self._contract, str(self._contract["name"]), path
                )
                if exposure.bus_backed
            ),
            None,
        )

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["subscribe_topics"])

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def project_request(self, request: ModelProjectionRequest) -> dict[str, Any]:
        data = dict(request.payload)
        topic = request.topic_or(self.subscribe_topics[0])
        meta = request.message_meta(topic)
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
            ModelExperimentResultProjectionRequest.model_validate(data)
        )
        row = result.row
        returned = await self.db.execute(
            _UPSERT,
            row.tenant_id,
            row.experiment_id,
            row.arm,
            row.model_id,
            row.engine,
            row.n,
            row.correct,
            row.accuracy,
            row.wilson_low,
            row.wilson_high,
            row.baseline_accuracy,
            row.advantage_over_baseline,
            row.clears_baseline,
            row.latency_p50_ms,
            row.latency_p90_ms,
            row.gpu_seconds,
            row.corpus_sha256,
            row.truth_sha256,
            row.report_ref,
            row.measured_at,
            tenant=str(row.tenant_id),
        )
        upserted = int(bool(returned))
        refused = int(not returned)
        if returned and self._snapshot_exposure is not None:
            await self.publish_snapshot_delta(
                self._snapshot_exposure,
                op="upsert",
                row=row.model_dump(mode="json"),
                source_event_id=meta.fallback_id,
                source_topic=meta.topic,
                source_partition=meta.partition,
                source_offset=meta.offset,
                tenant_id=str(row.tenant_id),
            )
        return {
            "rows_upserted": upserted,
            "rows_refused_by_ordering_guard": refused,
            "experiment_id": row.experiment_id,
            "arm": row.arm,
        }
