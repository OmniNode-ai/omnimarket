# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GET /v1/tenants/me/inference-credentials: the caller's own credential refs.

Reads the ``tenant_inference_credentials`` projection. A missing relation
degrades to an empty list (the projection not being materialised and the
projection holding no rows are the same observable state to a reader); an unset
source answers 503 and every other failure 500. The degrade is confined to this
read: the revoke route's ownership lookup never degrades.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from omnimarket.handlers.inference_credentials_http.bindings import (
    InferenceCredentialBindings,
)
from omnimarket.handlers.inference_credentials_http.errors import (
    CredentialReadModelMissingTableError,
    CredentialReadModelNotConfiguredError,
)
from omnimarket.models.model_inference_credential_http import (
    ModelInferenceCredentialHttpAnswer,
    ModelInferenceCredentialHttpCall,
    ModelInferenceCredentialListItem,
)

logger = logging.getLogger(__name__)

_FAILED = "failed to list inference credentials"
_COLUMNS = ("api_key_ref", "name", "provider", "created_at", "revoked_at")


class HandlerInferenceCredentialList:
    """List the caller tenant's credential refs."""

    def __init__(self, *, bindings: Callable[[], InferenceCredentialBindings]) -> None:
        self._bindings = bindings

    @staticmethod
    def _answer(status_code: int, body: object) -> ModelInferenceCredentialHttpAnswer:
        return ModelInferenceCredentialHttpAnswer(status_code=status_code, body=body)

    async def handle(
        self, request: ModelInferenceCredentialHttpCall
    ) -> ModelInferenceCredentialHttpAnswer:
        read_model = self._bindings().read_model
        if read_model is None:
            logger.error("The credential read model is not bound")
            return self._answer(503, {"detail": _FAILED})
        try:
            rows = await read_model.list_for_tenant(request.tenant_id)
        except CredentialReadModelMissingTableError:
            logger.error(
                "tenant_inference_credentials relation is absent -- serving an "
                "empty credential list for tenant %s",
                request.tenant_id,
            )
            return self._answer(200, [])
        except CredentialReadModelNotConfiguredError:
            logger.error(
                "The credential read model is not configured -- cannot read "
                "inference credentials for tenant %s",
                request.tenant_id,
            )
            return self._answer(503, {"detail": _FAILED})
        except Exception:
            logger.exception(
                "Failed to list inference credentials for tenant %s",
                request.tenant_id,
            )
            return self._answer(500, {"detail": _FAILED})
        try:
            items = [
                ModelInferenceCredentialListItem(
                    **{name: row[name] for name in _COLUMNS}
                ).model_dump(mode="json")
                for row in rows
            ]
        except Exception:
            logger.exception(
                "Unreadable credential row for tenant %s", request.tenant_id
            )
            return self._answer(500, {"detail": _FAILED})
        return self._answer(200, items)
