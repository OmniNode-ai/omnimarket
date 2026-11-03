# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DELETE /v1/tenants/me/inference-credentials/{api_key_ref}: revoke one ref.

Ownership is resolved against the projection first, scoped to the caller tenant,
before the store delete or the bus publish can run. A ref owned by another
tenant answers 404, identically to a ref that does not exist; a 403 would
confirm the ref's existence. Every lookup failure fails closed (500).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from omnimarket.handlers.inference_credentials_http.bindings import (
    InferenceCredentialBindings,
)
from omnimarket.models.model_inference_credential_http import (
    ModelInferenceCredentialHttpAnswer,
    ModelInferenceCredentialHttpCall,
)
from omnimarket.projection.credential_publisher import revoke_inference_credential

logger = logging.getLogger(__name__)

MAX_API_KEY_REF_LENGTH = 300
_LOOKUP_FAILED = "failed to look up inference credential"
_REVOKE_FAILED = "failed to revoke inference credential"


class HandlerInferenceCredentialRevoke:
    """Revoke one of the caller tenant's own credential refs."""

    def __init__(self, *, bindings: Callable[[], InferenceCredentialBindings]) -> None:
        self._bindings = bindings

    @staticmethod
    def _answer(status_code: int, body: object) -> ModelInferenceCredentialHttpAnswer:
        return ModelInferenceCredentialHttpAnswer(status_code=status_code, body=body)

    async def handle(
        self, request: ModelInferenceCredentialHttpCall
    ) -> ModelInferenceCredentialHttpAnswer:
        api_key_ref = request.path_params.get("api_key_ref", "")
        if not api_key_ref or len(api_key_ref) > MAX_API_KEY_REF_LENGTH:
            return self._answer(
                422,
                {
                    "detail": [
                        {
                            "type": "string_too_long",
                            "loc": ["path", "api_key_ref"],
                            "msg": "String should have at most "
                            f"{MAX_API_KEY_REF_LENGTH} characters",
                        }
                    ]
                },
            )
        ports = self._bindings()
        if ports.read_model is None:
            logger.error("The credential read model is not bound")
            return self._answer(500, {"detail": _LOOKUP_FAILED})
        try:
            owned = await ports.read_model.owned_by_tenant(
                api_key_ref, request.tenant_id
            )
        except Exception:
            logger.exception(
                "Failed to look up inference credential %s for tenant %s",
                api_key_ref,
                request.tenant_id,
            )
            return self._answer(500, {"detail": _LOOKUP_FAILED})
        if owned is None:
            return self._answer(404, {"detail": "inference credential not found"})
        if ports.secret_store is None or ports.event_bus is None:
            logger.error("Credential revoke ports are not bound")
            return self._answer(503, {"detail": _REVOKE_FAILED})
        try:
            if ports.revoke_outbox is not None:
                await ports.revoke_outbox.record(
                    api_key_ref=api_key_ref, tenant_id=request.tenant_id
                )
            revoked = await revoke_inference_credential(
                api_key_ref,
                tenant_id=request.tenant_id,
                secret_store=ports.secret_store,
                event_bus=ports.event_bus,
            )
        except Exception:
            logger.exception(
                "Failed to revoke inference credential %s for tenant %s",
                api_key_ref,
                request.tenant_id,
            )
            return self._answer(503, {"detail": _REVOKE_FAILED})
        return self._answer(200, revoked.model_dump(mode="json"))
