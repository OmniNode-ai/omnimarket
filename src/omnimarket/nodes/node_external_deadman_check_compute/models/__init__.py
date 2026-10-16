# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the external dead-man check (OMN-20808)."""

from .enum_external_deadman_action import EnumExternalDeadmanAction
from .enum_external_deadman_reason import EnumExternalDeadmanReason
from .model_external_deadman_backlog import ModelExternalDeadmanBacklog
from .model_external_deadman_canary import ModelExternalDeadmanCanary
from .model_external_deadman_check_request import ModelExternalDeadmanCheckRequest
from .model_external_deadman_check_result import ModelExternalDeadmanCheckResult
from .model_external_deadman_issue import ISSUE_TITLE, ModelExternalDeadmanIssue
from .model_external_deadman_mirror import (
    MIRROR_SCHEMA_VERSION,
    ModelExternalDeadmanMirror,
)
from .model_external_deadman_reason_finding import ModelExternalDeadmanReasonFinding

__all__ = [
    "ISSUE_TITLE",
    "MIRROR_SCHEMA_VERSION",
    "EnumExternalDeadmanAction",
    "EnumExternalDeadmanReason",
    "ModelExternalDeadmanBacklog",
    "ModelExternalDeadmanCanary",
    "ModelExternalDeadmanCheckRequest",
    "ModelExternalDeadmanCheckResult",
    "ModelExternalDeadmanIssue",
    "ModelExternalDeadmanMirror",
    "ModelExternalDeadmanReasonFinding",
]
