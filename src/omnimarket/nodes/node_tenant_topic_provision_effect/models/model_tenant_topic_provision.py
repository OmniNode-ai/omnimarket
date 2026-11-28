# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command and the typed answer of the tenant topic provision node.

One command names either one tenant slug or a backfill over every tenant the registry holds. The answer carries one
entry per tenant: the topics this run created, the ones the broker already held, the ones read back from the broker, and
-- when the set is not whole -- each topic that failed, by name. A tenant is ``provisioned`` only when every topic of the
set was read back from the broker; anything else is ``unprovisioned``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


class EnumTenantTopicState(StrEnum):
    """Whether a tenant's whole topic set was read back from the broker."""

    PROVISIONED = "provisioned"
    UNPROVISIONED = "unprovisioned"


class EnumTenantTopicOutcome(StrEnum):
    """The typed outcome on the completed (or failed) topic."""

    PROVISIONED = "provisioned"
    ALREADY_PROVISIONED = "already_provisioned"
    FAILED_TOPICS = "failed_topics"
    FAILED_BROKER_UNAVAILABLE = "failed_broker_unavailable"
    FAILED_NO_MIRROR_TOPICS = "failed_no_mirror_topics"
    FAILED_TENANT_SOURCE_UNAVAILABLE = "failed_tenant_source_unavailable"
    REFUSED_INVALID_SLUG = "refused_invalid_slug"


class ModelTenantTopicProvisionCommand(BaseModel):
    """Provision one tenant's topics, or backfill every tenant; exactly one of the two."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    tenant_slug: str | None = None
    backfill: bool = False

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        if (self.tenant_slug is None) == (not self.backfill):
            raise ValueError("name exactly one of tenant_slug or backfill=true")
        return self


class ModelTenantTopicReport(BaseModel):
    """What one run did for one tenant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_slug: str
    state: EnumTenantTopicState
    created: tuple[str, ...] = ()
    existing: tuple[str, ...] = ()
    read_back: tuple[str, ...] = ()
    failed_topics: tuple[str, ...] = ()
    #: One "<topic>: <error class>" line for each create the broker or the client refused; never a message.
    create_errors: tuple[str, ...] = ()
    detail: str = ""


class ModelTenantTopicProvisionResult(BaseModel):
    """The answer: the outcome, one line of detail and one report per tenant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    outcome: EnumTenantTopicOutcome
    detail: str
    tenants: tuple[ModelTenantTopicReport, ...] = ()
    emitted_at: datetime


__all__ = [
    "EnumTenantTopicOutcome",
    "EnumTenantTopicState",
    "ModelTenantTopicProvisionCommand",
    "ModelTenantTopicProvisionResult",
    "ModelTenantTopicReport",
]
