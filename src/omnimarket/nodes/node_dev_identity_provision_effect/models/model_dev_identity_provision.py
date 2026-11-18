# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev identity provision node's command and result (OMN-24115).

The result carries names and ids only: never a token, a secret or a password, and never the response's
``bootstrap_token``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumDevIdentityOutcome(StrEnum):
    """The typed outcomes of a dev identity provision run."""

    PROVISIONED = "provisioned"
    READY = "ready"
    REFUSED_NOT_DEV = "refused_not_dev"
    ADMIN_CREDENTIAL_MISSING = "admin_credential_missing"
    ADMIN_TOKEN_REFUSED = "admin_token_refused"
    KEYCLOAK_UNREACHABLE = "keycloak_unreachable"
    PASSWORD_UNAVAILABLE = "password_unavailable"
    USER_CREATE_FAILED = "user_create_failed"
    USER_TOKEN_REFUSED = "user_token_refused"
    PROVISION_REFUSED = "provision_refused"
    API_UNREACHABLE = "api_unreachable"
    PROVISION_RESPONSE_INVALID = "provision_response_invalid"


class ModelDevIdentityProvisionCommand(BaseModel):
    """The command a run or probe carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    write: bool


class ModelDevIdentityProvisionResult(BaseModel):
    """The typed result of a run; ``ok`` is true only for provisioned and ready."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    outcome: EnumDevIdentityOutcome
    detail: str
    realm: str
    lane: str
    user_name: str | None = None
    tenant_id: str | None = None
    tenant_slug: str | None = None
    expected_secret: str | None = None


__all__ = [
    "EnumDevIdentityOutcome",
    "ModelDevIdentityProvisionCommand",
    "ModelDevIdentityProvisionResult",
]
