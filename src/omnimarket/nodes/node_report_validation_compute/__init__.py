# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_report_validation_compute — deterministic dispatch-report validator (OMN-15163).

Validates a dispatched worker's raw final-report payload against the
role-resolved ``omnibase_core.models.dispatch.report`` contract (OMN-15161):
shape (pydantic construction, including placeholder/bare-ack content
rejection) AND content anchors (git-SHA / artifact-path claims, cross-checked
against ``node_report_anchor_probe_effect``'s (OMN-15164) typed probe output).
Pure COMPUTE -- no I/O in this handler; all anchor I/O lives in the EFFECT
sibling. Modeled on ``node_dispatch_worker``'s canonical def-B shape, NOT
``node_verified_dispatch_orchestrator`` (WARN-baselined dispatch()/dict-return
shape -- copying it hard-fails the OMN-14355 canon-shape ratchet).
"""

from omnimarket.nodes.node_report_validation_compute.handlers.handler_report_validation import (
    HandlerReportValidation,
)
from omnimarket.nodes.node_report_validation_compute.models import (
    ROLE_MAPPING_TABLE,
    UNMAPPABLE_DISPATCH_ROLES,
    EnumDispatchWorkerRole,
    EnumReportValidationVerdict,
    ModelReportValidationRequest,
    ModelReportValidationResult,
    resolve_report_role,
)


class NodeReportValidationCompute(HandlerReportValidation):
    """ONEX entry-point wrapper for HandlerReportValidation."""


__all__ = [
    "ROLE_MAPPING_TABLE",
    "UNMAPPABLE_DISPATCH_ROLES",
    "EnumDispatchWorkerRole",
    "EnumReportValidationVerdict",
    "HandlerReportValidation",
    "ModelReportValidationRequest",
    "ModelReportValidationResult",
    "NodeReportValidationCompute",
    "resolve_report_role",
]
