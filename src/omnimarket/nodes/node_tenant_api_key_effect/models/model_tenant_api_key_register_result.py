# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The register answer on its completed topic: ids, an outcome and times, never a key, a digest or a name."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_tenant_api_key_effect.models.enum_tenant_api_key_outcome import (
    EnumTenantApiKeyRegisterOutcome,
)


class ModelTenantApiKeyRegisterResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    tenant_id: UUID
    outcome: EnumTenantApiKeyRegisterOutcome
    credential_id: UUID | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None


__all__: list[str] = ["ModelTenantApiKeyRegisterResult"]
