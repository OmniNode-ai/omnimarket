# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerMeteringSummaryRefreshOnTerminal: a finished delegation refreshes its day (OMN-17427).

A delegation terminal asks for its tenant's UTC day and the all-time row. A
terminal with no tenant or no time asks for nothing: there is no tenant row or
day to put it in, and the schedule's whole refresh reads every tenant anyway.
"""

from __future__ import annotations

from datetime import UTC

from omnimarket.models.model_metering_summary_refresh_requested import (
    EnumMeteringRefreshReason,
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.nodes.node_metering_summary_refresh_compute.models import (
    ModelDelegationTerminalRefreshTrigger,
)


class HandlerMeteringSummaryRefreshOnTerminal:
    """COMPUTE handler: delegation terminal in, its tenant's day refresh out."""

    def handle(
        self, request: ModelDelegationTerminalRefreshTrigger
    ) -> ModelMeteringSummaryRefreshRequested | None:
        tenant = (request.tenant_id or "").strip()
        if not tenant or request.emitted_at is None:
            return None
        return ModelMeteringSummaryRefreshRequested(
            refresh_id=f"delegation-terminal-{request.correlation_id}",
            reason=EnumMeteringRefreshReason.DELEGATION_TERMINAL,
            tenant_ids=(tenant,),
            days=frozenset({request.emitted_at.astimezone(UTC).date()}),
            requested_at=request.emitted_at,
        )


__all__: list[str] = ["HandlerMeteringSummaryRefreshOnTerminal"]
