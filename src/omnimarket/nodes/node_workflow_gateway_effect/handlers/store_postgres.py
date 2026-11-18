# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real Postgres-backed workflow store."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_advisory_key import (
    advisory_lock_key,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_postgres_pool import (
    WorkflowPostgresPool,
    WorkflowTransaction,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_envelope import (
    ModelWorkflowCommandEnvelope,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_http import (
    ModelWorkflowCaller,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_store import (
    ModelRateLimitDecision,
    ModelWorkflowAdmission,
    ModelWorkflowQuotaStatus,
    ModelWorkflowRow,
)
from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_store import (
    RateLimitEventRecordError,
    WorkflowStoreUnavailableError,
)

logger = logging.getLogger(__name__)

_SQL_PLAN_CODE = (
    "SELECT plan_code FROM tenant_plans "
    "WHERE tenant_id = $1 AND is_active = true "
    "ORDER BY started_at DESC NULLS LAST LIMIT 1"
)

_SQL_DEFAULT_PLAN_CODE = (
    "SELECT code FROM plan_tiers WHERE is_default = true ORDER BY created_at LIMIT 1"
)

_SQL_ENTITLEMENT = (
    "SELECT limit_value, hard_cap FROM plan_entitlements "
    "WHERE plan_code = $1 AND metric_key = $2 AND window_key = $3 LIMIT 1"
)

_SQL_COUNT_IN_WINDOW = (
    "SELECT COUNT(*) FROM public.gateway_workflows "
    "WHERE tenant_id = $1 AND submitted_at >= $2 AND submitted_at < $3"
)

_SQL_INSERT_WORKFLOW = (
    "INSERT INTO public.gateway_workflows "
    "(workflow_id, tenant_id, principal_id, authenticated_user_id, workflow_type, "
    "contract_id, command_topic, envelope_id, correlation_id, causation_id, status) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'accepted')"
)

_SQL_SET_STATUS = (
    "UPDATE public.gateway_workflows SET status = $1, updated_at = now() "
    "WHERE workflow_id = $2 AND tenant_id = $3"
)

_SQL_FETCH_ROW = (
    "SELECT workflow_id, tenant_id, workflow_type, status, envelope_id, "
    "correlation_id, command_topic, submitted_at, updated_at, "
    "terminal_model_used, terminal_total_tokens, terminal_latency_ms, "
    "terminal_route, terminal_provider, terminal_credential_source, "
    "result_content, terminal_failure_class, terminal_failure_code, "
    "terminal_failure_reason, terminal_remediation, terminal_rule_evaluations "
    "FROM public.gateway_workflows WHERE workflow_id = $1 AND tenant_id = $2"
)

_SQL_RPM_ENTITLEMENT = (
    "SELECT limit_value FROM plan_entitlements "
    "WHERE plan_code = $1 AND metric_key = $2 AND window_key = $3 LIMIT 1"
)

_SQL_INSERT_RATE_LIMIT_EVENT = (
    "INSERT INTO public.gateway_rate_limit_events "
    "(event_id, tenant_id, principal_id, workflow_type, correlation_id, "
    "limit_per_minute, burst_capacity, retry_after_seconds) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
)


def _month_window(now: datetime) -> tuple[datetime, datetime]:
    """The UTC calendar month containing ``now``: [start, next month's start)."""
    now = now.astimezone(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class PostgresWorkflowStore:
    """Postgres implementation of ProtocolWorkflowStore over the hosted gateway schema."""

    def __init__(
        self,
        *,
        pool: WorkflowPostgresPool,
        metric_key: str,
        window_key: str,
        lock_namespace: str,
        rpm_metric_key: str,
        rpm_window_key: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._pool = pool
        self._metric_key = metric_key
        self._window_key = window_key
        self._lock_namespace = lock_namespace
        self._rpm_metric_key = rpm_metric_key
        self._rpm_window_key = rpm_window_key
        self._clock = clock

    @staticmethod
    async def _plan_code(
        reader: WorkflowPostgresPool | WorkflowTransaction, tenant_id: UUID
    ) -> str:
        """The tenant's active plan, else the default plan; neither is a store failure."""
        row = await reader.fetchrow(_SQL_PLAN_CODE, tenant_id)
        if row is None:
            row = await reader.fetchrow(_SQL_DEFAULT_PLAN_CODE)
        if row is None:
            raise WorkflowStoreUnavailableError("no active or default plan")
        return str(row[0])

    async def _quota(
        self, tx: WorkflowTransaction, tenant_id: UUID
    ) -> ModelWorkflowQuotaStatus:
        plan_code = await self._plan_code(tx, tenant_id)
        entitlement = await tx.fetchrow(
            _SQL_ENTITLEMENT, plan_code, self._metric_key, self._window_key
        )
        if entitlement is None:
            return ModelWorkflowQuotaStatus(status="ok", plan_code=plan_code)
        limit_value, hard_cap = int(entitlement[0]), bool(entitlement[1])
        start, end = _month_window(self._clock())
        counted = await tx.fetchrow(_SQL_COUNT_IN_WINDOW, tenant_id, start, end)
        used = int(counted[0]) if counted is not None else 0
        if used + 1 > limit_value:
            status = "hard_limit_hit" if hard_cap else "soft_limit"
        else:
            status = "ok"
        return ModelWorkflowQuotaStatus(
            status=status,
            plan_code=plan_code,
            limit_value=limit_value,
            used=used,
            remaining=max(0, limit_value - used),
            window_start=_iso_z(start),
            window_end=_iso_z(end),
        )

    async def admit(
        self, *, caller: ModelWorkflowCaller, envelope: ModelWorkflowCommandEnvelope
    ) -> ModelWorkflowAdmission:
        """Lock the tenant, read the monthly quota and insert the row in one transaction.

        The lock key is the hosted gateway's own, so a submission served here and one
        served there during the overlap serialize on the same tenant lock.
        """
        lock_key = advisory_lock_key(self._lock_namespace, str(caller.tenant_id))
        async with self._pool.transaction() as tx:
            await tx.execute("SELECT pg_advisory_xact_lock($1)", lock_key)
            quota = await self._quota(tx, caller.tenant_id)
            if quota.status == "hard_limit_hit":
                return ModelWorkflowAdmission(admitted=False, quota=quota)
            await tx.execute(
                _SQL_INSERT_WORKFLOW,
                envelope.workflow_id,
                caller.tenant_id,
                caller.principal_id,
                caller.authenticated_user_id,
                envelope.workflow_type,
                envelope.contract_id,
                envelope.command_topic,
                envelope.envelope_id,
                envelope.correlation_id,
                envelope.causation_id,
            )
            return ModelWorkflowAdmission(admitted=True, quota=quota)

    async def set_status(
        self, *, workflow_id: UUID, tenant_id: UUID, status: str
    ) -> None:
        try:
            await self._pool.execute(_SQL_SET_STATUS, status, workflow_id, tenant_id)
        except WorkflowStoreUnavailableError:
            raise
        except Exception as exc:
            raise WorkflowStoreUnavailableError(
                f"WorkflowStoreUnavailableError: {type(exc).__name__}"
            ) from exc

    async def fetch(
        self, *, workflow_id: UUID, tenant_id: UUID
    ) -> ModelWorkflowRow | None:
        try:
            row = await self._pool.fetchrow(_SQL_FETCH_ROW, workflow_id, tenant_id)
            if row is None:
                return None
            return ModelWorkflowRow(
                workflow_id=row[0],
                tenant_id=row[1],
                workflow_type=row[2],
                status=row[3],
                envelope_id=row[4],
                correlation_id=row[5],
                command_topic=row[6],
                submitted_at=row[7],
                updated_at=row[8],
                terminal_model_used=row[9],
                terminal_total_tokens=row[10],
                terminal_latency_ms=row[11],
                terminal_route=row[12],
                terminal_provider=row[13],
                terminal_credential_source=row[14],
                result_content=row[15],
                terminal_failure_class=row[16],
                terminal_failure_code=row[17],
                terminal_failure_reason=row[18],
                terminal_remediation=row[19],
                terminal_rule_evaluations=row[20],
            )
        except WorkflowStoreUnavailableError:
            raise
        except Exception as exc:
            raise WorkflowStoreUnavailableError(
                f"WorkflowStoreUnavailableError: {type(exc).__name__}"
            ) from exc

    async def rpm_override(self, *, tenant_id: UUID) -> int | None:
        try:
            plan_code = await self._plan_code(self._pool, tenant_id)
            row = await self._pool.fetchrow(
                _SQL_RPM_ENTITLEMENT,
                plan_code,
                self._rpm_metric_key,
                self._rpm_window_key,
            )
            if row is None:
                return None
            return int(row[0])
        except WorkflowStoreUnavailableError:
            raise
        except Exception as exc:
            raise WorkflowStoreUnavailableError(
                f"WorkflowStoreUnavailableError: {type(exc).__name__}"
            ) from exc

    async def record_rate_limit_exceeded(
        self,
        *,
        caller: ModelWorkflowCaller,
        workflow_type: str,
        correlation_id: UUID,
        decision: ModelRateLimitDecision,
    ) -> UUID:
        event_id = uuid4()
        try:
            await self._pool.execute(
                _SQL_INSERT_RATE_LIMIT_EVENT,
                event_id,
                caller.tenant_id,
                caller.principal_id,
                workflow_type,
                correlation_id,
                decision.refill_per_minute,
                decision.capacity,
                decision.retry_after_seconds,
            )
            return event_id
        except Exception as exc:
            raise RateLimitEventRecordError(
                f"RateLimitEventRecordError: {type(exc).__name__}"
            ) from exc


__all__: list[str] = ["PostgresWorkflowStore"]
