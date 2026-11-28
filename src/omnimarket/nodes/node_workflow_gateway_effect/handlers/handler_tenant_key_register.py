# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Signed-in provider key registration: the gateway writes a tenant's own key (OMN-26731).

The caller is the tenant the sign-in resolved to. The key is stored under that
tenant's UUID, never a slug and never a tenant the body names: a body that names
a tenant other than the caller is refused with nothing stored and nothing
published. The write is the hosted intake's own (``register_inference_credential``),
which builds its store with the intake's writer identity.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from omnibase_spi.protocols.services import ProtocolSecretStore
from pydantic import ValidationError

from omnimarket.handlers.routing.byok_model_discovery import (
    ModelByokModelDiscovery,
    discover_byok_model,
)
from omnimarket.handlers.routing.byok_plan_detection import (
    ModelByokPlanDetection,
    detect_byok_plan,
)
from omnimarket.handlers.routing.byok_provider_backends import (
    ByokPlanNotPermittedError,
)
from omnimarket.models.model_inference_credential_response import (
    ModelInferenceCredentialResponse,
)
from omnimarket.nodes.node_model_setup_effect.credential_output import (
    registered_credential_view,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.enum_workflow_refusal_code import (
    EnumWorkflowRefusalCode,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_http import (
    ModelTenantKeyRegisterCommand,
    ModelWorkflowHttpResponse,
)
from omnimarket.projection.credential_publisher import (
    CredentialKeyRefusedError,
    CredentialPlanUndeterminedError,
    CredentialStoreError,
    ModelInferenceCredentialCreateRequest,
    ProtocolCredentialEventBus,
    register_inference_credential,
)

logger = logging.getLogger(__name__)

#: A customer never binds a key to a lab host: that is operator tooling.
_REFUSED_FIELDS = ("bound_host",)

Register = Callable[..., Awaitable[ModelInferenceCredentialResponse]]


class HandlerTenantKeyRegister:
    """Registers one provider key for the signed-in tenant, and for no other."""

    def __init__(
        self,
        *,
        secret_store: ProtocolSecretStore | None = None,
        event_bus: ProtocolCredentialEventBus | None = None,
        plan_detector: Callable[
            ..., Awaitable[ModelByokPlanDetection]
        ] = detect_byok_plan,
        model_discoverer: Callable[
            ..., Awaitable[ModelByokModelDiscovery]
        ] = discover_byok_model,
        register: Register = register_inference_credential,
    ) -> None:
        self._secret_store = secret_store
        self._event_bus = event_bus
        self._plan_detector = plan_detector
        self._model_discoverer = model_discoverer
        self._register = register

    async def handle(
        self, request: ModelTenantKeyRegisterCommand
    ) -> ModelWorkflowHttpResponse:
        body: Any = request.body
        if not isinstance(body, dict):
            return _refusal(400, "request body must be a JSON object")
        fields = dict(body)
        claimed = fields.pop("tenant", None)
        caller = request.caller
        tenant_id = str(caller.tenant_id)
        if claimed is not None and claimed not in (tenant_id, caller.tenant_slug):
            return ModelWorkflowHttpResponse(
                status_code=403,
                body={
                    "detail": "a key can be registered for the signed-in tenant only, "
                    "not another tenant",
                    "code": EnumWorkflowRefusalCode.TENANT_FORBIDDEN.value,
                },
            )
        if any(name in fields for name in _REFUSED_FIELDS):
            return _refusal(422, "bound_host is operator-only and cannot be set here")
        try:
            create = ModelInferenceCredentialCreateRequest.model_validate(fields)
            response = await self._register(
                create,
                tenant_id=tenant_id,
                secret_store=self._secret_store,
                event_bus=self._event_bus,
                allow_custom_endpoint=False,
                plan_detector=self._plan_detector,
                model_discoverer=self._model_discoverer,
            )
        except ValidationError as exc:
            message = "; ".join(
                e["msg"] for e in exc.errors(include_input=False, include_url=False)
            )
            return _refusal(422, message)
        except (
            CredentialPlanUndeterminedError,
            CredentialKeyRefusedError,
            ByokPlanNotPermittedError,
            ValueError,
        ) as exc:
            return _refusal(422, str(exc))
        except CredentialStoreError as exc:
            logger.warning("tenant key store refused (%s)", type(exc).__name__)
            return _refusal(503, "key registration is temporarily unavailable")
        return ModelWorkflowHttpResponse(
            status_code=201, body=registered_credential_view(response, tenant_id)
        )


def _refusal(status: int, detail: str) -> ModelWorkflowHttpResponse:
    return ModelWorkflowHttpResponse(status_code=status, body={"detail": detail})


__all__: list[str] = ["HandlerTenantKeyRegister"]
