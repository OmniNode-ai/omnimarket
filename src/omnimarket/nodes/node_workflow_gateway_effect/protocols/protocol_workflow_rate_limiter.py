# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-principal token-bucket rate limiter behind the gateway."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_store import (
    ModelRateLimitDecision,
)


class WorkflowRateLimiterUnavailableError(RuntimeError):
    """The rate limiter could not be reached or is not configured."""


@runtime_checkable
class ProtocolWorkflowRateLimiter(Protocol):
    """Check whether a principal may submit another workflow now."""

    async def acquire(
        self,
        principal_id: str,
        *,
        refill_per_minute: int,
        capacity: int,
    ) -> ModelRateLimitDecision:
        """Consume one token for the principal and report the decision."""
        ...


__all__: list[str] = [
    "ProtocolWorkflowRateLimiter",
    "WorkflowRateLimiterUnavailableError",
]
