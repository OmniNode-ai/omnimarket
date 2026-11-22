# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The revoke command: one credential row of one tenant."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelTenantApiKeyRevokeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    correlation_id: UUID
    tenant_id: UUID
    credential_id: UUID


__all__: list[str] = ["ModelTenantApiKeyRevokeRequest"]
