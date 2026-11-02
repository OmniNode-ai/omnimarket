# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the complete ordered choices of the key selected for an overlay."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from omnimarket.handlers.tenant_identity_forms import sync_tenant_identity_forms
from omnimarket.routing.byok_provider_backends import BYOK_MODEL_UNRESOLVED
from omnimarket.tenant_credential_ref import is_tenant_credential_ref

if TYPE_CHECKING:
    from omnimarket.nodes.node_delegation_routing_reducer.tenant_key_selection import (
        ProtocolTenantOverlayReader,
    )

_logger = logging.getLogger("omnimarket.routing.tenant_overlay_resolver")


def selected_key_models(
    db: ProtocolTenantOverlayReader,
    *,
    tenant_id: str,
    provider: str | None,
    secret_ref: str | None,
    overlay_model: str,
) -> tuple[str, ...]:
    """Read only this key's choices; retain the existing unknown-key refusal."""
    if secret_ref is None or provider is None:
        return (overlay_model,)
    fallback = (
        BYOK_MODEL_UNRESOLVED if is_tenant_credential_ref(secret_ref) else overlay_model
    )
    try:
        # OMN-21819: a row stamped with the tenant's other form (name or registry
        # UUID) is the same tenant's key; the first form that holds it answers.
        rows: list[dict[str, object]] = []
        for tenant_form in sync_tenant_identity_forms(db, tenant_id):
            rows = db.query(
                "tenant_inference_credentials",
                filters={
                    "tenant_id": tenant_form,
                    "provider": provider,
                    "api_key_ref": secret_ref,
                },
            )
            if rows:
                break
    except Exception:
        _logger.warning(
            "selected credential choices unreadable for tenant_id=%s",
            tenant_id,
            exc_info=True,
        )
        return (fallback,)
    if not rows:
        return (fallback,)
    models = rows[0].get("models")
    if (
        isinstance(models, list)
        and models
        and all(isinstance(m, str) and m.strip() and len(m) <= 200 for m in models)
        and len(set(models)) == len(models)
    ):
        return tuple(str(model) for model in models)
    return (BYOK_MODEL_UNRESOLVED,)
