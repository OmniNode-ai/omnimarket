# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scaffold handler of node_retrospective_digest_render_compute: carries the crossing's identity fields through, decides nothing."""

from __future__ import annotations

from omnimarket.nodes.node_retrospective_digest_render_compute.models.model_retrospective_digest_render_compute_request import (
    ModelRetrospectiveDigestRenderComputeRequest,
)
from omnimarket.nodes.node_retrospective_digest_render_compute.models.model_retrospective_digest_render_compute_result import (
    ModelRetrospectiveDigestRenderComputeResult,
)


class HandlerRetrospectiveDigestRenderCompute:
    """Render the digest with its verdicts and candidate cards from the projected run.

    The scaffold copies the identity fields both crossings share and leaves every other
    field unset; the logic of this node arrives in the wave that fills its handler.
    """

    def handle(
        self, request: ModelRetrospectiveDigestRenderComputeRequest
    ) -> ModelRetrospectiveDigestRenderComputeResult:
        return ModelRetrospectiveDigestRenderComputeResult(
            run_id=request.run_id,
            correlation_id=request.correlation_id,
        )


__all__ = ["HandlerRetrospectiveDigestRenderCompute"]
