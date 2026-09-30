# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a effect writer for delegation evaluation labels and eval runs.

Prompt/response content lives on the lab table only because omnimarket is
public. Applied notifications contain row identities, never snapshot content.

A label event (OMN-19790) upserts ``delegation_eval_items``. A run-completed
event (OMN-19793) upserts ``delegation_eval_item_verdicts`` and
``delegation_eval_results`` under its eval run id, so a redelivered run adds
no row. A row the ordering guard declines (same or older ``observed_at``) is
counted in ``rows_refused_by_ordering_guard``, so a non-empty result never
reports zero rows handled.
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
from omnimarket.nodes.node_projection_delegation_eval.handlers.handler_projection_delegation_eval_run import (
    HandlerProjectionDelegationEvalRun,
)
from omnimarket.nodes.node_projection_delegation_eval.models import (
    ModelDelegationEvalProjectionRequest,
    ModelDelegationEvalRunProjectionRequest,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
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


_UPSERT_VERDICT = """
    INSERT INTO public.delegation_eval_item_verdicts (
        tenant_id, eval_run_id, item_key, manifest_id, gate_version, rater_role,
        rubric_version, task_class, stratum, label, recorded_verdict,
        recorded_deciding_check, replayed_verdict, replayed_deciding_check,
        replay_count, observed_at, first_seen_at, updated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, NOW(), NOW())
    ON CONFLICT (tenant_id, eval_run_id, item_key)
    DO UPDATE SET
        label = EXCLUDED.label,
        recorded_verdict = EXCLUDED.recorded_verdict,
        recorded_deciding_check = EXCLUDED.recorded_deciding_check,
        replayed_verdict = EXCLUDED.replayed_verdict,
        replayed_deciding_check = EXCLUDED.replayed_deciding_check,
        replay_count = EXCLUDED.replay_count,
        observed_at = EXCLUDED.observed_at,
        updated_at = NOW()
    WHERE public.delegation_eval_item_verdicts.observed_at < EXCLUDED.observed_at
    RETURNING projection_cursor
"""

_UPSERT_RESULT = """
    INSERT INTO public.delegation_eval_results (
        tenant_id, eval_run_id, task_class, stratum, arm, manifest_id,
        label_set_sha256, gate_version, rater_role, rubric_version, total_n,
        accepted_n, false_pass_count, false_pass_rate, false_pass_upper_bound,
        false_pass_wilson_low, false_pass_wilson_high, false_pass_line_verdict,
        false_pass_required_n, refused_n, false_refusal_count, false_refusal_rate,
        false_refusal_upper_bound, false_refusal_wilson_low, false_refusal_wilson_high,
        false_refusal_line_verdict, undetermined_n, undetermined_share, observed_at,
        first_seen_at, updated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
            $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29, NOW(), NOW())
    ON CONFLICT (tenant_id, eval_run_id, task_class, stratum, arm)
    DO UPDATE SET
        total_n = EXCLUDED.total_n,
        accepted_n = EXCLUDED.accepted_n,
        false_pass_count = EXCLUDED.false_pass_count,
        false_pass_rate = EXCLUDED.false_pass_rate,
        false_pass_upper_bound = EXCLUDED.false_pass_upper_bound,
        false_pass_wilson_low = EXCLUDED.false_pass_wilson_low,
        false_pass_wilson_high = EXCLUDED.false_pass_wilson_high,
        false_pass_line_verdict = EXCLUDED.false_pass_line_verdict,
        false_pass_required_n = EXCLUDED.false_pass_required_n,
        refused_n = EXCLUDED.refused_n,
        false_refusal_count = EXCLUDED.false_refusal_count,
        false_refusal_rate = EXCLUDED.false_refusal_rate,
        false_refusal_upper_bound = EXCLUDED.false_refusal_upper_bound,
        false_refusal_wilson_low = EXCLUDED.false_refusal_wilson_low,
        false_refusal_wilson_high = EXCLUDED.false_refusal_wilson_high,
        false_refusal_line_verdict = EXCLUDED.false_refusal_line_verdict,
        undetermined_n = EXCLUDED.undetermined_n,
        undetermined_share = EXCLUDED.undetermined_share,
        observed_at = EXCLUDED.observed_at,
        updated_at = NOW()
    WHERE public.delegation_eval_results.observed_at < EXCLUDED.observed_at
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
        self._derive_run = HandlerProjectionDelegationEvalRun()
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
            if _is_run_event(data):
                return await self._project_run(data, meta)
            written = await self._project_event(data, meta)
        finally:
            await self.db.close()
        return {"rows_upserted": len(written), "item_rows": written}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        if _is_run_event(data):
            await self._project_run(data, meta)
        else:
            await self._project_event(data, meta)
        return True

    async def _project_run(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        result = self._derive_run.handle(
            ModelDelegationEvalRunProjectionRequest.model_validate(data)
        )
        upserted = refused = 0
        for verdict in result.verdict_rows:
            returned = await self.db.execute(
                _UPSERT_VERDICT,
                verdict.tenant_id,
                verdict.eval_run_id,
                verdict.item_key,
                verdict.manifest_id,
                verdict.gate_version,
                verdict.rater_role,
                verdict.rubric_version,
                verdict.task_class,
                verdict.stratum,
                verdict.label,
                verdict.recorded_verdict,
                verdict.recorded_deciding_check,
                verdict.replayed_verdict,
                verdict.replayed_deciding_check,
                verdict.replay_count,
                verdict.observed_at,
                tenant=str(verdict.tenant_id),
            )
            upserted += int(bool(returned))
            refused += int(not returned)
        for row in result.result_rows:
            returned = await self.db.execute(
                _UPSERT_RESULT,
                row.tenant_id,
                row.eval_run_id,
                row.task_class,
                row.stratum,
                row.arm,
                row.manifest_id,
                row.label_set_sha256,
                row.gate_version,
                row.rater_role,
                row.rubric_version,
                row.total_n,
                row.accepted_n,
                row.false_pass_count,
                row.false_pass_rate,
                row.false_pass_upper_bound,
                row.false_pass_wilson_low,
                row.false_pass_wilson_high,
                row.false_pass_line_verdict,
                row.false_pass_required_n,
                row.refused_n,
                row.false_refusal_count,
                row.false_refusal_rate,
                row.false_refusal_upper_bound,
                row.false_refusal_wilson_low,
                row.false_refusal_wilson_high,
                row.false_refusal_line_verdict,
                row.undetermined_n,
                row.undetermined_share,
                row.observed_at,
                tenant=str(row.tenant_id),
            )
            upserted += int(bool(returned))
            refused += int(not returned)
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
            "eval_run_id": str(data["eval_run_id"]),
            "run_status": result.status,
            "verdict_rows": len(result.verdict_rows),
            "result_rows": len(result.result_rows),
        }

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


def _is_run_event(data: dict[str, Any]) -> bool:
    """A run-completed event carries an eval run id; a label event never does."""
    return "eval_run_id" in data
