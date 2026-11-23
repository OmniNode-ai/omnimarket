# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The wire shapes of the runtime's tenant provision command, as this node sends and reads them.

The tenant provision node owns the command and its answer. This node does not import that node's models: it holds the
two shapes it needs, and a seam test checks each against the owner's model, so a change on either side fails there.
The answer keeps only the fields this node reads and ignores the rest.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelTenantProvisionTopics(BaseModel):
    """The command topic, the completed topic and how long one run waits for the answer; the contract declares them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_topic: str
    completed_topic: str
    wait_seconds: float = Field(gt=0)


class ModelTenantProvisionRequest(BaseModel):
    """One ownerless tenant to create: the command id is the replay key, so the same user always asks the same thing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    tenant_slug: str = Field(min_length=1)
    name: str | None = None


class ModelTenantProvisionAnswer(BaseModel):
    """The runtime's answer on the completed topic; ``outcome`` is ``created`` or ``already_exists`` for a tenant."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    command_id: UUID
    outcome: str
    detail: str = ""
    tenant_id: str | None = None
    tenant_slug: str | None = None


__all__ = [
    "ModelTenantProvisionAnswer",
    "ModelTenantProvisionRequest",
    "ModelTenantProvisionTopics",
]
