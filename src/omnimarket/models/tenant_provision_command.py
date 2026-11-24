# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant provision command, shared by the tenant provision node and the gateway key provision node."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelTenantProvisionCommand(BaseModel):
    """Create one ownerless tenant. ``command_id`` is the caller's id for this command and the replay key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    tenant_slug: str = Field(min_length=1)
    name: str | None = None
    owner_email: str | None = None
    owner_username: str | None = None
    realm: str | None = None

    def carries_owner(self) -> bool:
        """True when any owner field is set; the first slice refuses such a command."""
        return any(
            value is not None
            for value in (self.owner_email, self.owner_username, self.realm)
        )


__all__ = ["ModelTenantProvisionCommand"]
