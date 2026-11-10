# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scaffold handler of node_retrospective_run_projection_effect: carries the crossing's identity fields through, decides nothing."""

from __future__ import annotations

from omnimarket.nodes.node_retrospective_run_projection_effect.models.model_retrospective_run_projection_effect_request import (
    ModelRetrospectiveRunProjectionEffectRequest,
)
from omnimarket.nodes.node_retrospective_run_projection_effect.models.model_retrospective_run_projection_effect_result import (
    ModelRetrospectiveRunProjectionEffectResult,
)


class HandlerRetrospectiveRunProjectionEffect:
    """Write the run's state to its projection and announce the projected revision.

    The scaffold copies the identity fields both crossings share and leaves every other
    field unset; the logic of this node arrives in the wave that fills its handler.
    """

    def handle(
        self, request: ModelRetrospectiveRunProjectionEffectRequest
    ) -> ModelRetrospectiveRunProjectionEffectResult:
        return ModelRetrospectiveRunProjectionEffectResult(
            run_id=request.run_id,
            correlation_id=request.correlation_id,
            revision=request.revision,
        )


__all__ = ["HandlerRetrospectiveRunProjectionEffect"]
