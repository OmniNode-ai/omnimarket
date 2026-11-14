# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev keycloak client node's command and result (OMN-24237).

The result carries names, ids and a status code only: never a token, a secret or a password, and never the proof
request's ``access_token``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumDevKeycloakClientOutcome(StrEnum):
    """The typed outcomes of a dev keycloak client run."""

    ENSURED = "ensured"
    READY = "ready"
    REFUSED_NOT_DEV = "refused_not_dev"
    REFUSED_ROLE_NOT_ALLOWED = "refused_role_not_allowed"
    BOOTSTRAP_CREDENTIAL_MISSING = "bootstrap_credential_missing"
    ADMIN_TOKEN_REFUSED = "admin_token_refused"
    KEYCLOAK_UNREACHABLE = "keycloak_unreachable"
    CLIENT_SECRET_UNAVAILABLE = "client_secret_unavailable"
    CLIENT_WRITE_FAILED = "client_write_failed"
    ROLE_GRANT_FAILED = "role_grant_failed"
    CLIENT_TOKEN_REFUSED = "client_token_refused"


class ModelDevKeycloakClientCommand(BaseModel):
    """The command a run or probe carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    write: bool


class ModelDevKeycloakClientResult(BaseModel):
    """The typed result of a run; ``ok`` is true only for ensured and ready."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    outcome: EnumDevKeycloakClientOutcome
    detail: str
    realm: str
    lane: str
    client_id: str | None = None
    token_status: int | None = None
    expected_location: str | None = None


__all__ = [
    "EnumDevKeycloakClientOutcome",
    "ModelDevKeycloakClientCommand",
    "ModelDevKeycloakClientResult",
]
