# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The caller tenant a runtime HTTP mount serves, as the hosted edge resolves it."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelHttpTenantIdentity(BaseModel):
    """One authenticated caller tenant, resolved from a request by the edge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID = Field(..., description="Canonical tenant UUID.")
    tenant_slug: str = Field(..., min_length=1, description="Tenant slug.")
    principal_id: str = Field(..., min_length=1, description="Caller principal id.")


__all__: list[str] = ["ModelHttpTenantIdentity"]
