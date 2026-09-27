# SPDX-License-Identifier: MIT
"""Pure, replay-deterministic latest-row fold for demo readiness."""

from __future__ import annotations

from omnimarket.nodes.node_projection_demo_readiness.models import (
    ModelDemoReadinessProjectionRequest,
    ModelDemoReadinessProjectionResult,
)


class HandlerProjectionDemoReadiness:
    """Accept only a strictly newer canonical source observation."""

    def handle(
        self, request: ModelDemoReadinessProjectionRequest
    ) -> ModelDemoReadinessProjectionResult:
        previous = request.previous_row
        observation = request.observation
        if previous is not None:
            if previous.node_id != observation.node_id:
                raise ValueError(
                    "previous row and observation must have the same node_id"
                )
            if observation.ordering_key <= previous.ordering_key:
                return ModelDemoReadinessProjectionResult(row=previous, applied=False)
        return ModelDemoReadinessProjectionResult(row=observation, applied=True)
