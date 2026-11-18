# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refusal and lifecycle status codes of the workflow gateway."""

from __future__ import annotations

from enum import StrEnum


class EnumWorkflowRefusalCode(StrEnum):
    """Why a submission or publish was refused."""

    CREDENTIAL_ABSENT = "auth.credential.absent"
    CREDENTIAL_INVALID = "auth.credential.invalid"
    TENANT_FORBIDDEN = "auth.tenant.forbidden"
    RESOURCE_NOT_FOUND = "resource.not_found"
    RESOURCE_CONFLICT = "resource.conflict"


class EnumWorkflowStatus(StrEnum):
    """Lifecycle status of a workflow row."""

    ACCEPTED = "accepted"
    PUBLISHED = "published"
    FAILED_PUBLISH = "failed_publish"
    COMPLETED = "completed"
    FAILED = "failed"


__all__: list[str] = [
    "EnumWorkflowRefusalCode",
    "EnumWorkflowStatus",
]
