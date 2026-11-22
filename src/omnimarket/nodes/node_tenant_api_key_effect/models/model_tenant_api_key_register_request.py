# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The register command: a tenant, a name and the sha256 digest of a key the caller holds (never the key)."""

from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict


class ModelTenantApiKeyRegisterRequest(BaseModel):
    """Shape is checked by the handler, not here, so a bad name or digest is a typed refusal and is never echoed."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    correlation_id: UUID
    tenant_id: UUID
    name: str
    key_hash: str
    expires_at: AwareDatetime | None = None


__all__: list[str] = ["ModelTenantApiKeyRegisterRequest"]
