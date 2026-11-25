# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refresh a tenant's metering rows from its stored runs (OMN-17427).

The shared lanes' refresh command (ModelMeteringSummaryRefreshRequested) names
tenants and days, never runs: a fold request carries every recorded run of the
tenant, and the dev lane's 47,751 runs would be an 11.7 MB message against the
bus's 1 MiB limit. So the writer reads the runs itself. For each tenant it
reads delegation_events under that tenant's row-level-security context, plans
one fold request exactly as the local end-of-delegate refresh does
(:func:`omnimarket.projection.sqlite_metering_summary.refresh_metering_after_terminal`),
and hands it to the writer's own fold-and-upsert, so both commands store rows
through one statement.

The tenants of a whole refresh are those the writer can see in
delegation_events with no tenant named: every tenant under a role that bypasses
row-level security, the lane's own tenant under one that does not. A named
tenant (a delegation terminal's) is always read under its own context. The
writer reads no cross-tenant registry: an in-process projection handler holds
one database identity, and the registry lives in another schema's binding. A
tenant with no runs is reported, never folded into an empty row.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, timedelta
from typing import Any, Protocol

from omnimarket.nodes.node_metering_summary_compute import (
    METERING_CAUSE_COLUMN,
    METERING_COLUMNS,
    ModelCounterfactualBaseline,
    ModelMeteringRecord,
)
from omnimarket.nodes.node_projection_metering_summary.models import (
    ModelMeteringSummaryFoldRequest,
)

# The fold's windows are half-open, [start, as_of): as_of is one microsecond
# past the newest run so the run is inside its own row, the same rule as the
# local refresh.
_AFTER_NEWEST_RUN = timedelta(microseconds=1)

VISIBLE_TENANTS_SQL = (
    "SELECT DISTINCT tenant_id::text AS tenant_id FROM public.delegation_events"
)
# OMN-19970: the dev seed's rows are never in a stored figure. OMN-23169: nor are
# typed synthetic (experiment) rows.
RECORDS_SQL = (
    f"SELECT {', '.join((*METERING_COLUMNS, METERING_CAUSE_COLUMN))} "
    "FROM public.delegation_events "
    "WHERE tenant_id::text = $1 AND data_source <> 'fixture' AND data_source <> 'experiment'"
)
STORED_ALL_SQL = (
    "SELECT pricing_manifest_version, as_of, summary_json FROM public.metering_summary "
    "WHERE tenant_id = $1 AND window_kind = 'all' AND window_start = '' "
    "AND baseline_model = $2"
)


class ProtocolMeteringStoreReads(Protocol):
    """The read side of the writer's database adapter."""

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        """Run ``query`` with the RLS tenant GUC set to ``tenant``."""
        ...


def plan_tenant_fold(
    *,
    tenant_id: str,
    records: Sequence[ModelMeteringRecord],
    days: frozenset[date] | None,
    baseline_model: str,
    baseline: ModelCounterfactualBaseline | None,
    stored_all_manifest_version: str | None,
    stored_all_present: bool,
) -> ModelMeteringSummaryFoldRequest | None:
    """The one fold request a tenant's refresh issues, or None with no runs.

    A requested day set narrows the fold only when the tenant's all row already
    exists under this baseline and pricing manifest; otherwise every day is
    refolded, so a first refresh backfills the tenant and a pricing change
    recomputes every row (ruling D-A).
    """
    if not records:
        return None
    version = baseline.pricing_manifest_version if baseline is not None else None
    whole = not stored_all_present or stored_all_manifest_version != version
    newest = max(r.occurred_at for r in records)
    return ModelMeteringSummaryFoldRequest(
        tenant_id=tenant_id,
        records=tuple(records),
        baseline=baseline,
        baseline_model=baseline_model,
        as_of=newest.astimezone(UTC) + _AFTER_NEWEST_RUN,
        days=None if whole or days is None else days,
    )


__all__: list[str] = [
    "RECORDS_SQL",
    "STORED_ALL_SQL",
    "VISIBLE_TENANTS_SQL",
    "ProtocolMeteringStoreReads",
    "plan_tenant_fold",
]
