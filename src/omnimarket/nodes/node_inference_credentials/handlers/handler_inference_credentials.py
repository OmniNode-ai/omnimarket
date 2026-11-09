# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Submit, list and revoke a tenant's inference credentials for a resolved caller.

Ported from the hosted onex-api's ``routers/inference_credentials.py`` (OMN-16405).
The caller is resolved first, by the identity resolver the hosted edge binds; no
read and no write runs for a caller that does not resolve. The cross-tenant rule is
kept exactly: the revoke route resolves ownership from the read model before the
publisher runs, and answers 404 (never 403) for a ref the caller does not own, so a
ref cannot be probed. A read of the list degrades an absent relation to an empty
list; the ownership lookup never degrades.

The customer's key value is read once, from the secret body, to validate it; it is
handed to the intake and appears in no response, log line or error detail.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from omnimarket.nodes.node_inference_credentials.handlers.identity_binding import (
    TenantIdentityResolverNotBoundError,
)
from omnimarket.nodes.node_inference_credentials.models.model_inference_credentials_http import (
    EnumInferenceCredentialsOperation,
    ModelInferenceCredentialsRequest,
    ModelInferenceCredentialsResponse,
)
from omnimarket.nodes.node_inference_credentials.protocols.protocol_inference_credential_intake import (
    ProtocolInferenceCredentialIntake,
)
from omnimarket.nodes.node_inference_credentials.protocols.protocol_inference_credential_read_model import (
    CredentialReadModelAbsentError,
    CredentialReadModelNotConfiguredError,
    ProtocolInferenceCredentialReadModel,
)
from omnimarket.projection.credential_publisher import (
    CredentialKeyRefusedError,
    CredentialPlanUndeterminedError,
    ModelInferenceCredentialCreateRequest,
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


def _answer(
    status_code: int, body: dict[str, object] | list[dict[str, object]]
) -> ModelInferenceCredentialsResponse:
    return ModelInferenceCredentialsResponse(status_code=status_code, body=body)


class HandlerInferenceCredentials:
    """Refuse before any read or write unless the caller resolves to a tenant."""

    def __init__(
        self,
        *,
        intake: ProtocolInferenceCredentialIntake,
        read_model: ProtocolInferenceCredentialReadModel,
        identity: ProtocolHttpTenantIdentity,
    ) -> None:
        self._intake = intake
        self._read_model = read_model
        self._identity = identity

    async def handle(
        self, request: ModelInferenceCredentialsRequest
    ) -> ModelInferenceCredentialsResponse:
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            caller = await self._identity.resolve(headers)
        except TenantIdentityResolverNotBoundError:
            return _answer(503, {"detail": "OIDC authentication is not configured"})
        except Exception:
            logger.exception("Caller identity resolution failed")
            return _answer(503, {"detail": "identity resolution unavailable"})
        if caller is None:
            presented = any(headers.get(name) for name in CREDENTIAL_HEADERS)
            return _answer(
                401,
                {
                    "detail": "Unauthorized",
                    "code": CREDENTIAL_INVALID if presented else CREDENTIAL_ABSENT,
                },
            )
        tenant_id = str(caller.tenant_id)
        match request.operation:
            case EnumInferenceCredentialsOperation.CREATE:
                return await self._create(request, tenant_id)
            case EnumInferenceCredentialsOperation.LIST:
                return await self._list(tenant_id)
            case EnumInferenceCredentialsOperation.REVOKE:
                return await self._revoke(request, tenant_id)

    async def _create(
        self, request: ModelInferenceCredentialsRequest, tenant_id: str
    ) -> ModelInferenceCredentialsResponse:
        if request.body is None:
            return _answer(422, {"detail": "request body required"})
        try:
            body = ModelInferenceCredentialCreateRequest.model_validate_json(
                request.body.get_secret_value()
            )
        except ValidationError as exc:
            # Never echo the input: it holds the customer's key.
            errors = exc.errors(
                include_input=False, include_url=False, include_context=False
            )
            return _answer(422, {"detail": [dict(e) for e in errors]})
        except Exception:
            logger.exception(
                "Failed to validate an inference credential for tenant %s", tenant_id
            )
            return _answer(503, {"detail": "failed to register inference credential"})
        try:
            created = await self._intake.register(body, tenant_id=tenant_id)
        except CredentialPlanUndeterminedError as exc:
            return _answer(
                422,
                {
                    "detail": str(exc),
                    "outcome": exc.outcome,
                    "plans": list(exc.plans),
                },
            )
        except CredentialKeyRefusedError as exc:
            return _answer(422, {"detail": str(exc), "outcome": exc.outcome})
        except Exception:
            logger.exception(
                "Failed to register inference credential for tenant %s", tenant_id
            )
            return _answer(503, {"detail": "failed to register inference credential"})
        return _answer(201, created.model_dump(mode="json"))

    async def _list(self, tenant_id: str) -> ModelInferenceCredentialsResponse:
        try:
            rows = await self._read_model.list_for_tenant(tenant_id)
        except CredentialReadModelAbsentError:
            # The projection not being materialized and the projection holding no
            # rows are the same state to a reader; the defect stays visible in the log.
            logger.error(
                "tenant_inference_credentials relation is absent -- serving an "
                "empty credential list for tenant %s",
                tenant_id,
            )
            return _answer(200, [])
        except CredentialReadModelNotConfiguredError:
            # A deployment gap, not an unmaterialized projection: never a healthy-looking [].
            logger.error(
                "no credential read model is configured -- cannot list for tenant %s",
                tenant_id,
            )
            return _answer(503, {"detail": "failed to list inference credentials"})
        except Exception:
            logger.exception(
                "Failed to list inference credentials for tenant %s", tenant_id
            )
            return _answer(500, {"detail": "failed to list inference credentials"})
        return _answer(200, [row.model_dump(mode="json") for row in rows])

    async def _revoke(
        self, request: ModelInferenceCredentialsRequest, tenant_id: str
    ) -> ModelInferenceCredentialsResponse:
        api_key_ref = request.api_key_ref
        if not api_key_ref:
            return _answer(422, {"detail": "api_key_ref required"})
        # Fails closed on every read failure: a missing relation and an unset
        # database both mean ownership could not be proven.
        try:
            owned = await self._read_model.owned_by_tenant(api_key_ref, tenant_id)
        except Exception:
            logger.exception(
                "Failed to look up inference credential %s for tenant %s",
                api_key_ref,
                tenant_id,
            )
            return _answer(500, {"detail": "failed to look up inference credential"})
        if owned is None:
            return _answer(404, {"detail": "inference credential not found"})
        try:
            revoked = await self._intake.revoke(api_key_ref, tenant_id=tenant_id)
        except Exception:
            logger.exception(
                "Failed to revoke inference credential %s for tenant %s",
                api_key_ref,
                tenant_id,
            )
            return _answer(503, {"detail": "failed to revoke inference credential"})
        return _answer(200, revoked.model_dump(mode="json"))


__all__: list[str] = ["HandlerInferenceCredentials"]
