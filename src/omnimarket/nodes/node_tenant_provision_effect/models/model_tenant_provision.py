# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Commands and answers of the tenant provision node, and the slug rule.

A provision command names the tenant slug and the command id; the answer carries a typed ``outcome``. Every refusal and
every handled error is an outcome on the ``-completed`` topic, never a missing answer, so a caller's one read sees every
result. The owner fields are optional: a gateway-key tenant needs no identity-provider user, and a command that carries
one is refused until the owner path exists.

The slug rule is stated here and nowhere else: a DNS label of 3 to 63 characters (lower-case letters, digits and
hyphens, starting and ending with a letter or digit), and not one of the reserved words. It is never derived from an
email.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.tenant_provision_command import ModelTenantProvisionCommand

SLUG_MIN_LENGTH = 3
SLUG_MAX_LENGTH = 63
_SLUG_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
RESERVED_SLUGS: frozenset[str] = frozenset(
    {
        "admin",
        "api",
        "app",
        "auth",
        "billing",
        "console",
        "dashboard",
        "default",
        "internal",
        "onex",
        "root",
        "status",
        "support",
        "system",
        "www",
    }
)


def slug_violation(slug: str) -> str | None:
    """The reason a slug is refused, or ``None`` when it is a usable tenant slug."""
    if not SLUG_MIN_LENGTH <= len(slug) <= SLUG_MAX_LENGTH:
        return f"slug length must be {SLUG_MIN_LENGTH} to {SLUG_MAX_LENGTH} characters"
    if _SLUG_PATTERN.fullmatch(slug) is None:
        return "slug must be a lower-case DNS label: letters, digits and hyphens, not starting or ending with a hyphen"
    if slug in RESERVED_SLUGS:
        return "slug is a reserved word"
    return None


class EnumTenantProvisionOutcome(StrEnum):
    """What a provision command ended as; every value but ``created`` and ``already_exists`` wrote nothing."""

    CREATED = "created"
    ALREADY_EXISTS = "already_exists"
    REFUSED_OWNER_NOT_SUPPORTED = "refused_owner_not_supported"
    REFUSED_SLUG_TAKEN = "refused_slug_taken"
    REFUSED_COMMAND_ID_REUSED = "refused_command_id_reused"
    REFUSED_INVALID_SLUG = "refused_invalid_slug"
    FAILED_NO_DEFAULT_PLAN = "failed_no_default_plan"
    FAILED_STORE_UNAVAILABLE = "failed_store_unavailable"


class EnumTenantReadOutcome(StrEnum):
    """What a read command ended as."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    REFUSED_INVALID_REQUEST = "refused_invalid_request"
    FAILED_STORE_UNAVAILABLE = "failed_store_unavailable"


class ModelTenantProvisionResult(BaseModel):
    """The answer on ``-completed``. The tenant fields are set for ``created`` and ``already_exists`` only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    outcome: EnumTenantProvisionOutcome
    detail: str
    tenant_id: UUID | None = None
    tenant_slug: str | None = None
    name: str | None = None
    provisioning_state: str | None = None
    plan_code: str | None = None
    created_at: datetime | None = None
    emitted_at: datetime


class ModelTenantReadCommand(BaseModel):
    """Read one tenant by ``tenant_id`` or by ``tenant_slug``; exactly one of the two."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    tenant_id: UUID | None = None
    tenant_slug: str | None = None


class ModelTenantReadResult(BaseModel):
    """The answer to a read: ``tenant_id, tenant_slug, name, provisioning_state, plan_code, created_at``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    outcome: EnumTenantReadOutcome
    detail: str
    tenant_id: UUID | None = None
    tenant_slug: str | None = None
    name: str | None = None
    provisioning_state: str | None = None
    plan_code: str | None = None
    created_at: datetime | None = None
    emitted_at: datetime


class EnumTenantOutboxFlushOutcome(StrEnum):
    """What one flush did: every outcome is an answer on the flush completed topic."""

    FLUSHED = "flushed"
    NOTHING_PENDING = "nothing_pending"
    PARTIAL = "partial"
    FAILED_STORE_UNAVAILABLE = "failed_store_unavailable"


class ModelTenantOutboxFlushCommand(BaseModel):
    """Flush the unpublished outbox rows. Extra keys are ignored on purpose: the provision completed event is a trigger too."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    command_id: UUID | None = None
    limit: int = Field(default=50, ge=1, le=500)


class ModelTenantOutboxFlushResult(BaseModel):
    """The answer to a flush: how many rows were published and how many were refused by the bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumTenantOutboxFlushOutcome
    detail: str
    published: int = 0
    failed: int = 0
    emitted_at: datetime


__all__ = [
    "RESERVED_SLUGS",
    "SLUG_MAX_LENGTH",
    "SLUG_MIN_LENGTH",
    "EnumTenantOutboxFlushOutcome",
    "EnumTenantProvisionOutcome",
    "EnumTenantReadOutcome",
    "ModelTenantOutboxFlushCommand",
    "ModelTenantOutboxFlushResult",
    "ModelTenantProvisionCommand",
    "ModelTenantProvisionResult",
    "ModelTenantReadCommand",
    "ModelTenantReadResult",
    "slug_violation",
]
