# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The revoke answer on its completed topic."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_tenant_api_key_effect.models.enum_tenant_api_key_outcome import (
    EnumTenantApiKeyRevokeOutcome,
)


class ModelTenantApiKeyRevokeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    tenant_id: UUID
    credential_id: UUID
    outcome: EnumTenantApiKeyRevokeOutcome


__all__: list[str] = ["ModelTenantApiKeyRevokeResult"]
