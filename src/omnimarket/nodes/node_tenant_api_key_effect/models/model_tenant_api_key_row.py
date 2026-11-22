# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One `tenant_api_keys` row as the register decision reads it: no digest, no secret."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelTenantApiKeyRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    credential_id: UUID
    tenant_id: UUID
    name: str
    is_active: bool
    expires_at: datetime | None


__all__: list[str] = ["ModelTenantApiKeyRow"]
