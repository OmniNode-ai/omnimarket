# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One row of GET /v1/tenants/me/inference-credentials: ref and metadata, never a value."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelInferenceCredentialListItem(BaseModel):
    """A credential ref the caller's tenant owns; ``revoked_at`` is None while active."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key_ref: str
    name: str
    provider: str
    created_at: datetime
    revoked_at: datetime | None = None


__all__: list[str] = ["ModelInferenceCredentialListItem"]
