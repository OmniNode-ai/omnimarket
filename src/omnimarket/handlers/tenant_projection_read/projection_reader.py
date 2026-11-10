# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tenant-scoped reads of the delegation and savings projections.

The tenant is one parsed UUID, bound under one name per column type. The
delegation projection keeps ``tenant_id`` as ``uuid`` and the savings projection
keeps it as ``text``; PostgreSQL has no ``uuid = text`` operator, so a predicate
binds ``tenant_uuid`` against a uuid column and ``tenant_text`` against a text
column. Nothing here casts a column. When a projection's column is converted its
predicate moves to the other name, and nothing else changes.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from omnimarket.models.model_tenant_delegation_projection import (
    ModelDelegationRunRow,
    ModelTenantSavingsAggregate,
)
from omnimarket.protocols.protocol_tenant_delegation_projection_reader import (
    ProjectionMissingError,
    ProtocolAnalyticsQueryExecutor,
)

logger = logging.getLogger(__name__)

SAVINGS_SQL = """
    SELECT
      COALESCE(
        (SELECT SUM(savings_usd) FROM savings_estimates
         WHERE tenant_id = %(tenant_text)s), 0
      )::float8 AS total_saved_usd,
      COALESCE(
        (SELECT SUM(local_cost_usd) FROM savings_estimates
         WHERE tenant_id = %(tenant_text)s), 0
      )::float8 AS total_spend_usd,
      COALESCE(
        (SELECT COUNT(*) FROM delegation_events
         WHERE tenant_id = %(tenant_uuid)s), 0
      )::bigint AS delegation_runs,
      (SELECT MAX(event_timestamp) FROM savings_estimates
       WHERE tenant_id = %(tenant_text)s) AS savings_latest,
      (SELECT MAX(timestamp) FROM delegation_events
       WHERE tenant_id = %(tenant_uuid)s) AS runs_latest
"""

# The LATERAL scopes the savings projection by the resolved tenant parameter, not
# by ``se.tenant_id = d.tenant_id``: that comparison spans the two projections'
# own tenant_id types. One latest matching savings row per run, never a sum, so a
# duplicate terminal delivery cannot multiply one run's savings.
RECENT_DELEGATIONS_SQL = """
    SELECT
      d.correlation_id AS run_id,
      d.task_type      AS task_type,
      d.delegated_to   AS model,
      d.quality_gate_passed AS quality_gate_passed,
      d.cost_usd       AS cost_usd,
      COALESCE(s.savings_usd, d.cost_savings_usd) AS saved_usd,
      d.timestamp      AS occurred_at
    FROM delegation_events d
    LEFT JOIN LATERAL (
        SELECT se.savings_usd
        FROM savings_estimates se
        WHERE se.session_id = d.correlation_id
          AND se.tenant_id = %(tenant_text)s
        ORDER BY se.event_timestamp DESC, se.updated_at DESC, se.id DESC
        LIMIT 1
    ) s ON true
    WHERE d.tenant_id = %(tenant_uuid)s
    ORDER BY d.timestamp DESC
    LIMIT %(limit)s
"""

# Savings is additive enrichment: when its projection is not provisioned yet the
# delegation row's own savings field is the source.
RECENT_DELEGATIONS_FALLBACK_SQL = """
    SELECT
      d.correlation_id AS run_id,
      d.task_type      AS task_type,
      d.delegated_to   AS model,
      d.quality_gate_passed AS quality_gate_passed,
      d.cost_usd       AS cost_usd,
      d.cost_savings_usd AS saved_usd,
      d.timestamp      AS occurred_at
    FROM delegation_events d
    WHERE d.tenant_id = %(tenant_uuid)s
    ORDER BY d.timestamp DESC
    LIMIT %(limit)s
"""


def tenant_params(tenant: UUID, **extra: Any) -> dict[str, Any]:
    """The one parsed tenant identity, bound under one name per column type."""
    params: dict[str, Any] = {
        "tenant_uuid": tenant,  # uuid columns
        "tenant_text": str(tenant),  # text columns
    }
    params.update(extra)
    return params


def _when(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None


class AnalyticsTenantProjectionReader:
    """``ProtocolTenantDelegationProjectionReader`` over an analytics query runner."""

    def __init__(self, runner: ProtocolAnalyticsQueryExecutor) -> None:
        self._runner = runner

    async def read_savings(self, tenant_id: UUID) -> ModelTenantSavingsAggregate:
        rows = await self._runner.run_query(
            SAVINGS_SQL, tenant_params(tenant_id), tenant_id=str(tenant_id)
        )
        row = rows[0] if rows else {}
        return ModelTenantSavingsAggregate(
            total_saved_usd=float(row.get("total_saved_usd") or 0.0),
            total_spend_usd=float(row.get("total_spend_usd") or 0.0),
            delegation_runs=int(row.get("delegation_runs") or 0),
            savings_latest=_when(row.get("savings_latest")),
            runs_latest=_when(row.get("runs_latest")),
        )

    async def read_recent_delegations(
        self, tenant_id: UUID, limit: int
    ) -> tuple[ModelDelegationRunRow, ...]:
        params = tenant_params(tenant_id, limit=limit)
        try:
            rows = await self._runner.run_query(
                RECENT_DELEGATIONS_SQL, params, tenant_id=str(tenant_id)
            )
        except ProjectionMissingError:
            logger.info("savings projection absent; retrying from delegation truth")
            # A second missing table is delegation_events itself: it propagates.
            rows = await self._runner.run_query(
                RECENT_DELEGATIONS_FALLBACK_SQL, params, tenant_id=str(tenant_id)
            )
        return tuple(
            ModelDelegationRunRow(
                run_id=str(r.get("run_id") or ""),
                task_type=str(r.get("task_type") or ""),
                model=str(r.get("model") or ""),
                quality_gate_passed=bool(r.get("quality_gate_passed")),
                cost_usd=float(r.get("cost_usd") or 0.0),
                saved_usd=float(r.get("saved_usd") or 0.0),
                occurred_at=_when(r.get("occurred_at")),
            )
            for r in rows
        )
