# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scaffold handler of node_retrospective_audit_compute: carries the crossing's identity fields through, decides nothing."""

from __future__ import annotations

from omnimarket.nodes.node_retrospective_audit_compute.models.model_retrospective_audit_compute_request import (
    ModelRetrospectiveAuditComputeRequest,
)
from omnimarket.nodes.node_retrospective_audit_compute.models.model_retrospective_audit_compute_result import (
    ModelRetrospectiveAuditComputeResult,
)


class HandlerRetrospectiveAuditCompute:
    """Audit the calculated metrics against their independent intake source.

    The scaffold copies the identity fields both crossings share and leaves every other
    field unset; the logic of this node arrives in the wave that fills its handler.
    """

    def handle(
        self, request: ModelRetrospectiveAuditComputeRequest
    ) -> ModelRetrospectiveAuditComputeResult:
        return ModelRetrospectiveAuditComputeResult(
            run_id=request.run_id,
            correlation_id=request.correlation_id,
            input_hash=request.input_hash,
        )


__all__ = ["HandlerRetrospectiveAuditCompute"]
