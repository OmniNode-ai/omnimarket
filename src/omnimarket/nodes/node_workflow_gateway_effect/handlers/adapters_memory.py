# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory adapters of the gateway's store, rate limiter and publisher."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

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
from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_publisher import (
    WorkflowPublishError,
)
from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_store import (
    RateLimitEventRecordError,
    WorkflowStoreUnavailableError,
)


class InProcessTokenBucketLimiter:
    """Per-principal token bucket with the same maths as the Lua script."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    async def acquire(
        self,
        principal_id: str,
        *,
        refill_per_minute: int,
        capacity: int,
    ) -> ModelRateLimitDecision:
        now = self._clock()
        tokens, last_ts = self._buckets.get(principal_id, (float(capacity), now))
        elapsed = max(0.0, now - last_ts)
        tokens = min(float(capacity), tokens + elapsed * (refill_per_minute / 60.0))
        if tokens >= 1.0:
            tokens -= 1.0
            allowed = True
            retry_after = 0
        else:
            allowed = False
            retry_after = max(1, math.ceil((1.0 - tokens) * 60.0 / refill_per_minute))
        self._buckets[principal_id] = (tokens, now)
        return ModelRateLimitDecision(
            allowed=allowed,
            remaining=max(0, math.floor(tokens)),
            capacity=capacity,
            refill_per_minute=refill_per_minute,
            retry_after_seconds=retry_after,
        )


class InMemoryWorkflowStore:
    """Dict-backed store for the in-memory profile and tests."""

    def __init__(
        self,
        *,
        monthly_limit: int | None,
        hard_cap: bool,
        clock: Callable[[], datetime],
        rpm: int | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self._monthly_limit = monthly_limit
        self._hard_cap = hard_cap
        self._clock = clock
        self._rpm = rpm
        self._fail_with = fail_with
        self._rows: dict[tuple[UUID, UUID], ModelWorkflowRow] = {}
        self.rate_limit_events: list[UUID] = []
        self.calls: list[str] = []

    def _guard(self, method: str) -> None:
        self.calls.append(method)
        if self._fail_with is not None:
            if method == "record_rate_limit_exceeded":
                raise RateLimitEventRecordError(str(self._fail_with))
            raise WorkflowStoreUnavailableError(str(self._fail_with))

    def _count_month(self, tenant_id: UUID) -> int:
        now = self._clock()
        return sum(
            1
            for (tid, _), row in self._rows.items()
            if tid == tenant_id
            and row.submitted_at.year == now.year
            and row.submitted_at.month == now.month
        )

    async def admit(
        self,
        *,
        caller: ModelWorkflowCaller,
        envelope: ModelWorkflowCommandEnvelope,
    ) -> ModelWorkflowAdmission:
        self._guard("admit")
        used = self._count_month(caller.tenant_id)
        limit = self._monthly_limit
        status: Literal["ok", "soft_limit", "hard_limit_hit"] = "ok"
        if limit is not None and used + 1 > limit:
            status = "hard_limit_hit" if self._hard_cap else "soft_limit"
        admitted = status != "hard_limit_hit"
        if admitted:
            now = self._clock()
            row = ModelWorkflowRow(
                workflow_id=envelope.workflow_id,
                tenant_id=caller.tenant_id,
                workflow_type=envelope.workflow_type,
                status="accepted",
                envelope_id=envelope.envelope_id,
                correlation_id=envelope.correlation_id,
                command_topic=envelope.command_topic,
                submitted_at=now,
                updated_at=now,
            )
            self._rows[(caller.tenant_id, envelope.workflow_id)] = row
        quota = ModelWorkflowQuotaStatus(
            status=status,
            limit_value=limit,
            used=used,
            remaining=max(0, limit - used) if limit is not None else None,
        )
        return ModelWorkflowAdmission(admitted=admitted, quota=quota)

    async def set_status(
        self,
        *,
        workflow_id: UUID,
        tenant_id: UUID,
        status: str,
    ) -> None:
        self._guard("set_status")
        key = (tenant_id, workflow_id)
        if key not in self._rows:
            return
        row = self._rows[key]
        self._rows[key] = row.model_copy(
            update={"status": status, "updated_at": self._clock()}
        )

    async def fetch(
        self,
        *,
        workflow_id: UUID,
        tenant_id: UUID,
    ) -> ModelWorkflowRow | None:
        self._guard("fetch")
        return self._rows.get((tenant_id, workflow_id))

    async def rpm_override(self, *, tenant_id: UUID) -> int | None:
        self._guard("rpm_override")
        return self._rpm

    async def record_rate_limit_exceeded(
        self,
        *,
        caller: ModelWorkflowCaller,
        workflow_type: str,
        correlation_id: UUID,
        decision: ModelRateLimitDecision,
    ) -> UUID:
        self._guard("record_rate_limit_exceeded")
        event_id = uuid4()
        self.rate_limit_events.append(event_id)
        return event_id

    def apply_terminal(self, workflow_id: UUID, **terminal_fields: Any) -> None:
        """Test helper: set terminal fields on a stored row."""
        for key, row in self._rows.items():
            if key[1] == workflow_id:
                self._rows[key] = row.model_copy(update=terminal_fields)
                return


class InMemoryWorkflowPublisher:
    """Records published messages; raises when ``fail_with`` is set."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self._fail_with = fail_with
        self.published: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(
        self,
        *,
        topic: str,
        key: str,
        wire: dict[str, Any],
    ) -> None:
        if self._fail_with is not None:
            raise WorkflowPublishError(str(self._fail_with))
        self.published.append((topic, key, wire))


__all__: list[str] = [
    "InMemoryWorkflowPublisher",
    "InMemoryWorkflowStore",
    "InProcessTokenBucketLimiter",
]
