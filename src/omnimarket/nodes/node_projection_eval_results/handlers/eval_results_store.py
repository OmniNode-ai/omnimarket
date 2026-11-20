# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The SQL of the eval-results projection (OMN-24171): what the writer reads and writes.

Every statement names ``tenant_id`` and runs under the caller-resolved tenant, so the table's
row-level-security policy decides it. An item is replaced in SQL only by a later ``scored_at``,
or at an equal one by the greater ``outcome_digest``, so concurrent consumers agree with the
fold whatever the arrival order. A trace part is inserted and never replaced.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    ModelEvalProjectionResult,
    ModelEvalReadPlan,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    RATE_KEY_COLUMNS,
    ModelEvalItemRow,
    ModelEvalRateRow,
    ModelEvalRunRecord,
    ModelEvalRunSequence,
    ModelEvalTraceRow,
)

ITEM_COLUMNS: tuple[str, ...] = tuple(ModelEvalItemRow.model_fields)
"""Every field of the stored item, in order: the columns ``eval_item_scores`` must hold."""

_ITEM_JSON = frozenset({"field_scores"})
_TRACE_COLUMNS = tuple(ModelEvalTraceRow.model_fields)
_RUN_COLUMNS = tuple(ModelEvalRunRecord.model_fields)
_RUN_JSON = frozenset({"sequences", "band_usable", "band_target"})
_RATE_COLUMNS = tuple(ModelEvalRateRow.model_fields)
_RATE_JSON = frozenset({"band_usable", "band_target", "scorer_versions"})
_UUID_COLUMNS = frozenset({"tenant_id", "event_id", "scoring_id"})


def _placeholders(columns: tuple[str, ...], json_columns: frozenset[str]) -> str:
    return ", ".join(
        f"${i}::jsonb" if name in json_columns else f"${i}"
        for i, name in enumerate(columns, start=1)
    )


def _build_upsert_item() -> str:
    updates = ",\n        ".join(
        f"{name} = EXCLUDED.{name}"
        for name in ITEM_COLUMNS
        if name not in ("tenant_id", "item_key")
    )
    return (
        f"INSERT INTO public.eval_item_scores ({', '.join(ITEM_COLUMNS)})\n"
        f"VALUES ({_placeholders(ITEM_COLUMNS, _ITEM_JSON)})\n"
        "ON CONFLICT (tenant_id, item_key)\n"
        f"DO UPDATE SET\n        {updates},\n        updated_at = NOW()\n"
        "WHERE (public.eval_item_scores.scored_at, public.eval_item_scores.outcome_digest)\n"
        "    < (EXCLUDED.scored_at, EXCLUDED.outcome_digest)\n"
        "RETURNING projection_cursor"
    )


UPSERT_ITEM = _build_upsert_item()

INSERT_TRACE = (
    f"INSERT INTO public.eval_item_traces ({', '.join(_TRACE_COLUMNS)})\n"
    f"VALUES ({_placeholders(_TRACE_COLUMNS, frozenset())})\n"
    "ON CONFLICT (tenant_id, item_key, scoring_id, part_index) DO NOTHING\n"
    "RETURNING projection_cursor"
)
DELETE_TRACE_PARTS = (
    "DELETE FROM public.eval_item_traces WHERE tenant_id = $1 AND scoring_id = $2"
)
UPSERT_RUN = (
    f"INSERT INTO public.eval_run_closes ({', '.join(_RUN_COLUMNS)})\n"
    f"VALUES ({_placeholders(_RUN_COLUMNS, _RUN_JSON)})\n"
    "ON CONFLICT (tenant_id, run_id)\n"
    "DO UPDATE SET\n        "
    + ",\n        ".join(
        f"{name} = EXCLUDED.{name}"
        for name in _RUN_COLUMNS
        if name not in ("tenant_id", "run_id")
    )
    + ",\n        updated_at = NOW()\n"
    "WHERE public.eval_run_closes.closed_at < EXCLUDED.closed_at\n"
    "RETURNING projection_cursor"
)
DELETE_RATES_OF_RUN = (
    "DELETE FROM public.eval_run_rates WHERE tenant_id = $1 AND run_id = $2"
)
DELETE_RATES_OF_RUN_MODEL = DELETE_RATES_OF_RUN + " AND model_id = $3"
INSERT_RATE = (
    f"INSERT INTO public.eval_run_rates ({', '.join(_RATE_COLUMNS)})\n"
    f"VALUES ({_placeholders(_RATE_COLUMNS, _RATE_JSON)})"
)
_SELECT_ITEMS = (
    f"SELECT {', '.join(ITEM_COLUMNS)} FROM public.eval_item_scores "
    "WHERE tenant_id = $1 AND run_id = $2"
)
SELECT_ITEMS_OF_RUN = _SELECT_ITEMS
SELECT_ITEMS_OF_RUN_MODEL = _SELECT_ITEMS + " AND model_id = $3"
SELECT_RUN = (
    f"SELECT {', '.join(_RUN_COLUMNS)} FROM public.eval_run_closes "
    "WHERE tenant_id = $1 AND run_id = $2"
)
SELECT_TRACE_PARTS = (
    f"SELECT {', '.join(_TRACE_COLUMNS)} FROM public.eval_item_traces "
    "WHERE tenant_id = $1 AND scoring_id = $2"
)
SELECT_SNAPSHOT_RATES = (
    f"SELECT {', '.join(_RATE_COLUMNS)} FROM public.eval_run_rates "
    "WHERE tenant_id = $1 AND (series = 'tracking' OR run_state = 'live')"
)
SELECT_SNAPSHOT_RUNS = (
    f"SELECT {', '.join(_RUN_COLUMNS)} FROM public.eval_run_closes "
    "WHERE tenant_id = $1 AND series = 'tracking' AND void_reason <> ''"
)


def _json_plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_json_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_plain(v) for k, v in value.items()}
    return value


def _param(name: str, value: Any, json_columns: frozenset[str]) -> Any:
    if name in _UUID_COLUMNS:
        return value if isinstance(value, UUID) else UUID(str(value))
    if name in json_columns:
        return json.dumps(_json_plain(value), sort_keys=True)
    if name == "engine" and value is None:
        return ""
    return getattr(value, "value", value)


def _params(
    model: Any, columns: tuple[str, ...], json_columns: frozenset[str]
) -> tuple[Any, ...]:
    return tuple(_param(name, getattr(model, name), json_columns) for name in columns)


def item_params(row: ModelEvalItemRow) -> tuple[Any, ...]:
    return _params(row, ITEM_COLUMNS, _ITEM_JSON)


def _loaded(record: dict[str, Any], json_columns: frozenset[str]) -> dict[str, Any]:
    loaded = dict(record)
    for name in json_columns & loaded.keys():
        if isinstance(loaded[name], str):
            loaded[name] = json.loads(loaded[name])
    if "tenant_id" in loaded:
        loaded["tenant_id"] = str(loaded["tenant_id"])
    return loaded


class EvalResultsStore:
    """Reads and writes the four stores through a loop-bound database adapter."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def load(
        self, plan: ModelEvalReadPlan
    ) -> tuple[
        tuple[ModelEvalItemRow, ...],
        ModelEvalRunRecord | None,
        tuple[ModelEvalTraceRow, ...],
    ]:
        tenant = UUID(plan.tenant_id)
        items: tuple[ModelEvalItemRow, ...] = ()
        record: ModelEvalRunRecord | None = None
        parts: tuple[ModelEvalTraceRow, ...] = ()
        if plan.run_id:
            query, args = (
                (SELECT_ITEMS_OF_RUN_MODEL, (tenant, plan.run_id, plan.model_id))
                if plan.model_id
                else (SELECT_ITEMS_OF_RUN, (tenant, plan.run_id))
            )
            items = tuple(
                ModelEvalItemRow.model_validate(_loaded(r, _ITEM_JSON))
                for r in await self._db.execute(query, *args, tenant=plan.tenant_id)
            )
            for r in await self._db.execute(
                SELECT_RUN, tenant, plan.run_id, tenant=plan.tenant_id
            ):
                loaded = _loaded(r, _RUN_JSON)
                loaded["sequences"] = tuple(
                    ModelEvalRunSequence.model_validate(s) for s in loaded["sequences"]
                )
                record = ModelEvalRunRecord.model_validate(loaded)
        if plan.scoring_id:
            parts = tuple(
                ModelEvalTraceRow.model_validate(_loaded(r, frozenset()))
                for r in await self._db.execute(
                    SELECT_TRACE_PARTS,
                    tenant,
                    UUID(plan.scoring_id),
                    tenant=plan.tenant_id,
                )
            )
        return items, record, parts

    async def apply(
        self, plan: ModelEvalReadPlan, result: ModelEvalProjectionResult
    ) -> dict[str, int]:
        """Write what the fold decided; returns the counts of rows that changed a table."""
        tenant = plan.tenant_id
        counts = {"items": 0, "traces": 0, "run": 0, "rates": 0}
        for scoring in result.drop_scoring_ids:
            await self._db.execute(
                DELETE_TRACE_PARTS, UUID(tenant), UUID(scoring), tenant=tenant
            )
        for item in result.items:
            returned = await self._db.execute(
                UPSERT_ITEM, *item_params(item), tenant=str(item.tenant_id)
            )
            counts["items"] += int(bool(returned))
        for part in result.traces:
            returned = await self._db.execute(
                INSERT_TRACE,
                *_params(part, _TRACE_COLUMNS, frozenset()),
                tenant=part.tenant_id,
            )
            counts["traces"] += int(bool(returned))
        if result.run_record is not None:
            returned = await self._db.execute(
                UPSERT_RUN,
                *_params(result.run_record, _RUN_COLUMNS, _RUN_JSON),
                tenant=result.run_record.tenant_id,
            )
            counts["run"] += int(bool(returned))
        if plan.run_id and (result.rates or result.items or result.run_record):
            rate_tenant = result.rates[0].tenant_id if result.rates else tenant
            delete, delete_args = (
                (
                    DELETE_RATES_OF_RUN_MODEL,
                    (UUID(rate_tenant), plan.run_id, plan.model_id),
                )
                if plan.model_id
                else (DELETE_RATES_OF_RUN, (UUID(rate_tenant), plan.run_id))
            )
            await self._db.execute_in_transaction(
                [(delete, delete_args)]
                + [
                    (INSERT_RATE, _params(rate, _RATE_COLUMNS, _RATE_JSON))
                    for rate in result.rates
                ],
                tenant=rate_tenant,
            )
            counts["rates"] += len(result.rates)
        return counts

    async def snapshot_inputs(
        self, tenant_id: str
    ) -> tuple[tuple[ModelEvalRateRow, ...], tuple[ModelEvalRunRecord, ...]]:
        tenant = UUID(tenant_id)
        rates = []
        for r in await self._db.execute(
            SELECT_SNAPSHOT_RATES, tenant, tenant=tenant_id
        ):
            loaded = _loaded(r, _RATE_JSON)
            loaded["engine"] = loaded["engine"] or None
            loaded["scorer_versions"] = tuple(loaded["scorer_versions"])
            rates.append(ModelEvalRateRow.model_validate(loaded))
        records = []
        for r in await self._db.execute(SELECT_SNAPSHOT_RUNS, tenant, tenant=tenant_id):
            loaded = _loaded(r, _RUN_JSON)
            loaded["sequences"] = tuple(
                ModelEvalRunSequence.model_validate(s) for s in loaded["sequences"]
            )
            records.append(ModelEvalRunRecord.model_validate(loaded))
        return tuple(rates), tuple(records)


__all__ = [
    "ITEM_COLUMNS",
    "RATE_KEY_COLUMNS",
    "EvalResultsStore",
    "item_params",
]
