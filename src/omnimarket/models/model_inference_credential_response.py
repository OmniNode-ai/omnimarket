# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public credential registration response; never carries secret values."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_credential_expiry_warning import EnumCredentialExpiryWarning


class ModelInferenceCredentialResponse(BaseModel):
    """Response body of the create route -- ref + metadata, NEVER the value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key_ref: str
    name: str
    provider: str
    #: OMN-20157. The plan the credential was registered under: the one the
    #: customer named or detection found. ``None`` for a provider with a single
    #: plan, where there was nothing to choose.
    plan: str | None = None
    #: OMN-20157. The model the key resolved from the provider's own model list,
    #: which the route runs. ``None`` when the list could not be read at
    #: registration; the effect then resolves it at the first delegation.
    model: str | None = None
    models: list[str] = Field(default_factory=list)
    base_url: str | None = None
    connection_id: str | None = None
    expected_version: int | None = Field(default=None, strict=True, gt=0)
    #: OMN-20991. The expiry recorded with the credential, if one was given.
    expires_at: datetime | None = None
    #: OMN-20991. Set when the expiry is absent or is not January 1. A warning,
    #: never a refusal: the key is stored and routed either way.
    expiry_warning: EnumCredentialExpiryWarning | None = None
    #: OMN-21012. The lab host the key is bound to, if any.
    bound_host: str | None = None
    #: OMN-20967. The Anthropic workspace the key belongs to, if any.
    workspace_id: str | None = None
    created_at: datetime
