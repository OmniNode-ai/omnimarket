# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One URL-bound backend authority for tenant intake and both projections."""

from __future__ import annotations

import re

from omnimarket.handlers.routing.byok_provider_backends import (
    ModelByokProviderBackend,
    resolve_byok_provider_backend,
)
from omnimarket.models.model_endpoint_models import (
    ModelEndpointModelsRequest,
)


def registered_endpoint_backend(
    provider: str,
    *,
    plan: str | None = None,
    base_url: object = None,
    connection_id: object = None,
) -> ModelByokProviderBackend | None:
    backend = resolve_byok_provider_backend(provider, plan=plan)
    if base_url is None and connection_id is None:
        return backend
    if (
        not isinstance(base_url, str)
        or not isinstance(connection_id, str)
        or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", connection_id) is None
    ):
        raise ValueError(
            "A custom endpoint requires a valid base URL and connection identity"
        )
    if backend is None or not backend.endpoint_from_registration:
        raise ValueError("This provider does not allow a registered endpoint")
    base = ModelEndpointModelsRequest(url=base_url).url
    return backend.model_copy(
        update={
            "endpoint_url": base + "/v1/chat/completions",
            "models_url": base + "/v1/models",
            "backend_id": backend.backend_id + "-" + connection_id,
        }
    )
