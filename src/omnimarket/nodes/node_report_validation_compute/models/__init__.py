# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the report-validation COMPUTE node (OMN-15163)."""

from omnimarket.nodes.node_report_validation_compute.models.model_dispatch_worker_role import (
    EnumDispatchWorkerRole,
)
from omnimarket.nodes.node_report_validation_compute.models.model_report_validation_request import (
    ModelReportValidationRequest,
)
from omnimarket.nodes.node_report_validation_compute.models.model_report_validation_result import (
    ModelReportValidationResult,
)
from omnimarket.nodes.node_report_validation_compute.models.model_report_validation_verdict import (
    EnumReportValidationVerdict,
)
from omnimarket.nodes.node_report_validation_compute.models.model_role_mapping import (
    ROLE_MAPPING_TABLE,
    UNMAPPABLE_DISPATCH_ROLES,
    resolve_report_role,
)

__all__ = [
    "ROLE_MAPPING_TABLE",
    "UNMAPPABLE_DISPATCH_ROLES",
    "EnumDispatchWorkerRole",
    "EnumReportValidationVerdict",
    "ModelReportValidationRequest",
    "ModelReportValidationResult",
    "resolve_report_role",
]
