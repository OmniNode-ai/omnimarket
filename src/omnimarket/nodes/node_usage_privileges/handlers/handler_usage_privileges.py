# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""List the usage privilege catalog (GET /v1/usage/privileges) for a resolved caller."""

from __future__ import annotations

import logging

from omnimarket.nodes.node_usage_privileges.handlers.identity_binding import (
    TenantIdentityResolverNotBoundError,
)
from omnimarket.nodes.node_usage_privileges.models.model_usage_privileges_http import (
    ModelUsagePrivilegesRequest,
    ModelUsagePrivilegesResponse,
)
from omnimarket.nodes.node_usage_privileges.protocols.protocol_usage_privilege_repository import (
    ProtocolUsagePrivilegeRepository,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)

logger = logging.getLogger(__name__)

# Refusal envelope of the hosted edge: the unchanged detail plus a code that tells
# "you sent nothing" (absent) from "what you sent did not resolve" (invalid).
CREDENTIAL_ABSENT = "auth.credential.absent"
CREDENTIAL_INVALID = "auth.credential.invalid"
CREDENTIAL_HEADERS = ("x-api-key", "authorization")


class HandlerUsagePrivileges:
    """Refuse before any catalog read unless the caller resolves to a tenant."""

    def __init__(
        self,
        *,
        repository: ProtocolUsagePrivilegeRepository,
        identity: ProtocolHttpTenantIdentity,
    ) -> None:
        self._repository = repository
        self._identity = identity

    async def handle(
        self, request: ModelUsagePrivilegesRequest
    ) -> ModelUsagePrivilegesResponse:
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            caller = await self._identity.resolve(headers)
        except TenantIdentityResolverNotBoundError:
            return ModelUsagePrivilegesResponse(
                status_code=503,
                body={"detail": "OIDC authentication is not configured"},
            )
        except Exception:
            logger.exception("Caller identity resolution failed")
            return ModelUsagePrivilegesResponse(
                status_code=503, body={"detail": "identity resolution unavailable"}
            )
        if caller is None:
            presented = any(headers.get(name) for name in CREDENTIAL_HEADERS)
            return ModelUsagePrivilegesResponse(
                status_code=401,
                body={
                    "detail": "Unauthorized",
                    "code": CREDENTIAL_INVALID if presented else CREDENTIAL_ABSENT,
                },
            )
        try:
            privileges = await self._repository.fetch_usage_privileges()
        except Exception:
            logger.exception("Failed to list usage privileges")
            return ModelUsagePrivilegesResponse(
                status_code=500, body={"detail": "failed to list usage privileges"}
            )
        return ModelUsagePrivilegesResponse(
            status_code=200, body=[p.model_dump(mode="json") for p in privileges]
        )


__all__: list[str] = ["HandlerUsagePrivileges"]
