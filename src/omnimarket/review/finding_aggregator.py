# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared review boundary for finding aggregation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from omnimarket.nodes.node_finding_aggregator_compute.handlers.handler_finding_aggregator import (
    HandlerFindingAggregator,
)
from omnimarket.nodes.node_finding_aggregator_compute.models.model_finding_aggregator_input import (
    ModelFindingAggregatorInput,
    ModelSourceFindings,
)
from omnimarket.nodes.node_finding_aggregator_compute.models.model_finding_aggregator_output import (
    ModelFindingAggregatorOutput,
)


class FindingAggregatorGateway:
    """Public review-facing adapter for node_finding_aggregator_compute."""

    def __init__(self, handler: HandlerFindingAggregator | None = None) -> None:
        self._handler = handler or HandlerFindingAggregator()

    async def aggregate(
        self,
        *,
        correlation_id: UUID,
        findings_by_model: Mapping[str, Sequence[dict[str, object]]],
    ) -> ModelFindingAggregatorOutput:
        sources = tuple(
            ModelSourceFindings(model_name=model_key, findings=tuple(findings))
            for model_key, findings in findings_by_model.items()
        )
        return await self._handler.handle(
            ModelFindingAggregatorInput(
                correlation_id=correlation_id,
                sources=sources,
            )
        )


__all__ = ["FindingAggregatorGateway"]
