# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Postgres-backed store behind the gateway workflow effect node."""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_envelope import (
    ModelWorkflowCommandEnvelope,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_http import (
    ModelWorkflowCaller,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_store import (
    ModelRateLimitDecision,
    ModelWorkflowAdmission,
    ModelWorkflowRow,
)


class WorkflowStoreUnavailableError(RuntimeError):
    """The store could not be reached or is not configured."""


class RateLimitEventRecordError(RuntimeError):
    """A rate-limit event could not be recorded."""


@runtime_checkable
class ProtocolWorkflowStore(Protocol):
    """Admission, status, and read access to the gateway workflow store."""

    async def admit(
        self,
        *,
        caller: ModelWorkflowCaller,
        envelope: ModelWorkflowCommandEnvelope,
    ) -> ModelWorkflowAdmission:
        """Under one tenant-scoped lock and one transaction: read the monthly quota, and when not hard-limited insert the submission row with status the caller's envelope implies ('accepted'); return whether it was admitted and the quota reading."""
        ...

    async def set_status(
        self,
        *,
        workflow_id: UUID,
        tenant_id: UUID,
        status: str,
    ) -> None:
        """Move a workflow row to a new status."""
        ...

    async def fetch(
        self,
        *,
        workflow_id: UUID,
        tenant_id: UUID,
    ) -> ModelWorkflowRow | None:
        """Read one workflow row by id and tenant, or None when absent."""
        ...

    async def rpm_override(self, *, tenant_id: UUID) -> int | None:
        """The tenant's plan requests-per-minute entitlement, or None."""
        ...

    async def record_rate_limit_exceeded(
        self,
        *,
        caller: ModelWorkflowCaller,
        workflow_type: str,
        correlation_id: UUID,
        decision: ModelRateLimitDecision,
    ) -> UUID:
        """Record a rate-limit rejection and return the event id."""
        ...


__all__: list[str] = [
    "ProtocolWorkflowStore",
    "RateLimitEventRecordError",
    "WorkflowStoreUnavailableError",
]
