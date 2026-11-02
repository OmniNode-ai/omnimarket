# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scaffold handler of node_retrospective_collect_effect: carries the crossing's identity fields through, decides nothing."""

from __future__ import annotations

from omnimarket.nodes.node_retrospective_collect_effect.models.model_retrospective_collect_effect_request import (
    ModelRetrospectiveCollectEffectRequest,
)
from omnimarket.nodes.node_retrospective_collect_effect.models.model_retrospective_collect_effect_result import (
    ModelRetrospectiveCollectEffectResult,
)


class HandlerRetrospectiveCollectEffect:
    """Collect the week's evidence records into one immutable snapshot.

    The scaffold copies the identity fields both crossings share and leaves every other
    field unset; the logic of this node arrives in the wave that fills its handler.
    """

    def handle(
        self, request: ModelRetrospectiveCollectEffectRequest
    ) -> ModelRetrospectiveCollectEffectResult:
        return ModelRetrospectiveCollectEffectResult(
            run_id=request.run_id,
            correlation_id=request.correlation_id,
        )


__all__ = ["HandlerRetrospectiveCollectEffect"]
