# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refresh a tenant's metering rows from the runs created since its last fold (OMN-21288).

The store refresh used to read and refold every stored run of the tenant on each
delegation terminal: 139,891 runs, 3 to 8 s of CPU, inside the one runtime
process that also serves the projection API and /health. This continues from
the stored ``all`` row instead. Its ``as_of`` is the watermark (one microsecond
past the newest run folded) and its ``summary_json`` is the folded state, so a
refresh reads the runs created at or after the watermark, folds only those, and
adds them to the stored summaries of the ``all`` window and of the days they
fall in. The cost is the new runs, whatever the tenant has stored.

A whole fold of every stored run happens in two cases only: the tenant has
nothing to continue from (no stored ``all`` row, or one folded at another
pricing manifest or baseline, which a rebuild must recompute), or the command
asks for it (a reason named in the contract's ``full_rebuild_reasons``, never a
delegation terminal's). A run created before the watermark that reaches the
store late is not in the incremental path's view; the rebuild on demand folds
it in.

Every CPU-bound step (row to record conversion, the fold) goes through the
injected ``run_blocking``, so it runs in a worker thread and the loop that
awaits the refresh keeps serving.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Sequence
from typing import Any

from omnimarket.models.model_metering_summary_refresh_requested import (
    EnumMeteringRefreshReason,
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.nodes.node_metering_summary_compute import (
    ModelCounterfactualBaseline,
    ModelMeteringRecord,
    metering_record_from_row,
)
from omnimarket.nodes.node_projection_metering_summary.baseline import resolve_baseline
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_incremental_fold import (
    ModelMeteringFoldState,
    add_records,
    merge_stored_days,
    state_from_continuation,
    state_rows,
    stored_continuation,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_store_refresh import (
    RECORDS_SQL,
    STORED_ALL_SQL,
    VISIBLE_TENANTS_SQL,
    ProtocolMeteringStoreReads,
)
from omnimarket.nodes.node_projection_metering_summary.models import (
    ModelMeteringSummaryRow,
)
from omnimarket.pricing import resolve_baseline_model

# The runs created at or after the watermark. The watermark is one microsecond
# past the newest folded run, so no folded run is read twice.
INCREMENTAL_RECORDS_SQL = f"{RECORDS_SQL} AND created_at >= $2"
STORED_DAYS_SQL = (
    "SELECT window_start, summary_json FROM public.metering_summary "
    "WHERE tenant_id = $1 AND baseline_model = $2 AND window_kind = 'day' "
    "AND window_start = ANY($3::text[])"
)

RunBlocking = Callable[..., Awaitable[Any]]


def _records(rows: Sequence[Any]) -> list[ModelMeteringRecord]:
    return [r for r in map(metering_record_from_row, rows) if r is not None]


async def _tenants(
    request: ModelMeteringSummaryRefreshRequested,
    reads_for: Callable[[str], ProtocolMeteringStoreReads],
) -> list[str]:
    if request.tenant_ids is not None:
        return sorted(set(request.tenant_ids))
    rows = await reads_for("delegation_events").execute(VISIBLE_TENANTS_SQL)
    return sorted({str(row["tenant_id"]) for row in rows if row.get("tenant_id")})


async def refresh_incrementally(
    request: ModelMeteringSummaryRefreshRequested,
    *,
    reads_for: Callable[[str], ProtocolMeteringStoreReads],
    store_rows: Callable[[Sequence[ModelMeteringSummaryRow]], Awaitable[int]],
    run_blocking: RunBlocking,
    full_rebuild_reasons: Collection[EnumMeteringRefreshReason],
    chunk_records: int,
) -> dict[str, Any]:
    """Refresh each requested tenant's rows, folding only what is new where it can.

    ``chunk_records`` bounds how many runs one worker-thread step converts and
    folds, so a whole fold holds a chunk and the windows, never every run.
    """
    if chunk_records < 1:
        raise ValueError("chunk_records must be at least 1")
    baseline_model = resolve_baseline_model(overlay={}, store={}).model
    baseline = resolve_baseline(baseline_model)
    rebuild_asked = request.reason in full_rebuild_reasons
    refreshed: list[str] = []
    rebuilt: list[str] = []
    unchanged: list[str] = []
    without_runs: list[str] = []
    upserted = 0
    records_folded = 0
    for tenant in await _tenants(request, reads_for):
        stored = await reads_for("metering_summary").execute(
            STORED_ALL_SQL, tenant, baseline_model, tenant=tenant
        )
        continuation = (
            None
            if rebuild_asked
            else stored_continuation(stored[0] if stored else None, baseline=baseline)
        )
        if continuation is None:
            rows = await reads_for("delegation_events").execute(
                RECORDS_SQL, tenant, tenant=tenant
            )
            state = ModelMeteringFoldState()
        else:
            rows = await reads_for("delegation_events").execute(
                INCREMENTAL_RECORDS_SQL, tenant, continuation.watermark, tenant=tenant
            )
            state = state_from_continuation(continuation)
        for start in range(0, len(rows), chunk_records):
            await run_blocking(
                _fold_chunk, state, rows[start : start + chunk_records], baseline
            )
        records_folded += state.records_folded
        if state.records_folded == 0:
            (without_runs if continuation is None else unchanged).append(tenant)
            continue
        if continuation is not None:
            # The new runs name their days only once folded; the stored day rows
            # they extend are read then, and only those.
            day_rows = await reads_for("metering_summary").execute(
                STORED_DAYS_SQL,
                tenant,
                baseline_model,
                [day.isoformat() for day in sorted(state.touched)],
                tenant=tenant,
            )
            await run_blocking(
                merge_stored_days,
                state,
                {str(r["window_start"]): str(r["summary_json"]) for r in day_rows},
            )
        upserted += await store_rows(
            state_rows(
                state,
                tenant_id=tenant,
                baseline=baseline,
                baseline_model=baseline_model,
            )
        )
        refreshed.append(tenant)
        if continuation is None:
            rebuilt.append(tenant)
    return {
        "refresh_id": request.refresh_id,
        "reason": request.reason.value,
        "baseline_model": baseline_model,
        "tenants_refreshed": refreshed,
        "tenants_rebuilt_in_full": rebuilt,
        "tenants_unchanged": unchanged,
        "tenants_without_runs": without_runs,
        "rows_upserted": upserted,
        "records_folded": records_folded,
    }


def _fold_chunk(
    state: ModelMeteringFoldState,
    rows: Sequence[Any],
    baseline: ModelCounterfactualBaseline | None,
) -> None:
    add_records(state, _records(rows), baseline=baseline)


__all__: list[str] = [
    "INCREMENTAL_RECORDS_SQL",
    "STORED_DAYS_SQL",
    "refresh_incrementally",
]
