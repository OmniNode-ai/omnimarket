# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One catalog row of a tenant's key pool, reduced to what the alert may read."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelKeyPoolCredentialRow(BaseModel):
    """A ``tenant_inference_credentials`` row, minus every secret-bearing column.

    The catalog row also carries a key fingerprint. It is deliberately not
    declared here, and the model ignores undeclared columns: whatever else a
    reader hands over, a fingerprint or a value can never reach the alert text
    because there is no field to hold it. A name and a reference are the only
    identity the alert prints.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    api_key_ref: str = Field(..., min_length=1, description="Reference, never a value.")
    tenant_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, description="Operator-chosen key name.")
    revoked_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = Field(
        default=None, description="None means the key carries no expiry (undated)."
    )


__all__ = ["ModelKeyPoolCredentialRow"]
