# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""POST /v1/tenants/me/inference-credentials: register one BYOK credential.

A thin wrapper over ``credential_publisher.register_inference_credential`` with
every adapter injected. The tenant is the edge-resolved caller and is never read
from the body. The reference is derived deterministically from the tenant, the
caller's ``Idempotency-Key`` and the canonical body, so a double submit or a
retry after a lost response converges on one secret and one projection row.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from omnimarket.handlers.inference_credentials_http.bindings import (
    REF_HMAC_SECRET_NAME,
    InferenceCredentialBindings,
)
from omnimarket.handlers.inference_credentials_http.redaction import (
    render_validation_error,
    unparseable_body_detail,
)
from omnimarket.handlers.routing.byok_provider_backends import (
    ByokPlanNotPermittedError,
    byok_routable_plans,
)
from omnimarket.models.model_inference_credential_create_request import (
    ModelInferenceCredentialCreateRequest,
)
from omnimarket.models.model_inference_credential_http import (
    ModelInferenceCredentialHttpAnswer,
    ModelInferenceCredentialHttpCall,
)
from omnimarket.projection.credential_publisher import (
    CredentialKeyRefusedError,
    CredentialPlanUndeterminedError,
    register_inference_credential,
)

logger = logging.getLogger(__name__)

PLAN_UNDETERMINED_CODE = "BYOK_PLAN_UNDETERMINED"
KEY_REFUSED_CODE = "BYOK_KEY_REFUSED"
_FAILED = "failed to register inference credential"


def derive_api_key_ref(
    *, hmac_key: bytes, tenant_id: str, provider: str, idempotency_key: str, body: Any
) -> str:
    """``cred_{tenant}_{provider}_{HMAC}`` over the key and the canonical body."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    message = b"\n".join(
        [
            tenant_id.encode(),
            idempotency_key.encode(),
            hashlib.sha256(canonical).hexdigest().encode(),
        ]
    )
    digest = hmac.new(hmac_key, message, hashlib.sha256).hexdigest()[:32]
    return f"cred_{tenant_id}_{provider}_{digest}"


def _key_refusal(
    exc: CredentialPlanUndeterminedError
    | ByokPlanNotPermittedError
    | CredentialKeyRefusedError,
) -> ModelInferenceCredentialHttpAnswer:
    """The typed 422 for a key registration refused as the customer's input."""
    if isinstance(exc, CredentialPlanUndeterminedError):
        detail: dict[str, Any] = {
            "code": PLAN_UNDETERMINED_CODE,
            "outcome": exc.outcome,
            "message": str(exc),
            "plans": list(exc.plans),
        }
    elif isinstance(exc, ByokPlanNotPermittedError):
        detail = {
            "code": exc.code,
            "plan": exc.plan,
            "message": exc.message,
            "plans": list(byok_routable_plans(exc.provider)),
        }
    else:
        detail = {"code": KEY_REFUSED_CODE, "outcome": exc.outcome, "message": str(exc)}
    return ModelInferenceCredentialHttpAnswer(status_code=422, body={"detail": detail})


class HandlerInferenceCredentialCreate:
    """Register one credential for the caller tenant."""

    def __init__(self, *, bindings: Callable[[], InferenceCredentialBindings]) -> None:
        self._bindings = bindings

    @staticmethod
    def _answer(status_code: int, detail: Any) -> ModelInferenceCredentialHttpAnswer:
        return ModelInferenceCredentialHttpAnswer(
            status_code=status_code, body={"detail": detail}
        )

    async def handle(
        self, request: ModelInferenceCredentialHttpCall
    ) -> ModelInferenceCredentialHttpAnswer:
        if request.body_unparseable or not isinstance(request.body, dict):
            return self._answer(422, unparseable_body_detail())
        try:
            body = ModelInferenceCredentialCreateRequest.model_validate(request.body)
        except ValidationError as exc:
            return self._answer(
                422,
                render_validation_error(
                    exc, request.body, ModelInferenceCredentialCreateRequest
                ),
            )
        except RuntimeError:
            logger.exception("Credential intake could not judge the request")
            return self._answer(503, _FAILED)
        if not (request.idempotency_key or "").strip():
            return self._answer(400, "Idempotency-Key header is required")

        ports = self._bindings()
        if (
            ports.secret_store is None
            or ports.event_bus is None
            or ports.secret_source is None
        ):
            logger.error("Credential intake ports are not bound")
            return self._answer(503, _FAILED)
        try:
            hmac_value = await ports.secret_source.get(REF_HMAC_SECRET_NAME)
            if not hmac_value:
                raise RuntimeError("reference key is not mapped")
            api_key_ref = derive_api_key_ref(
                hmac_key=hmac_value.encode(),
                tenant_id=request.tenant_id,
                provider=body.provider,
                idempotency_key=(request.idempotency_key or "").strip(),
                body=request.body,
            )
            optional: dict[str, Any] = {}
            if ports.plan_detector is not None:
                optional["plan_detector"] = ports.plan_detector
            if ports.model_discoverer is not None:
                optional["model_discoverer"] = ports.model_discoverer
            registered = await register_inference_credential(
                body,
                tenant_id=request.tenant_id,
                secret_store=ports.secret_store,
                event_bus=ports.event_bus,
                api_key_ref=api_key_ref,
                **optional,
            )
        except (
            CredentialPlanUndeterminedError,
            ByokPlanNotPermittedError,
            CredentialKeyRefusedError,
        ) as exc:
            return _key_refusal(exc)
        except Exception:
            logger.exception(
                "Failed to register inference credential for tenant %s",
                request.tenant_id,
            )
            return self._answer(503, _FAILED)
        return ModelInferenceCredentialHttpAnswer(
            status_code=201, body=registered.model_dump(mode="json")
        )
