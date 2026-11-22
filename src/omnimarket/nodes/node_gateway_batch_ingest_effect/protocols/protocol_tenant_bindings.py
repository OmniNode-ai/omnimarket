# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant registry seam: the binding of a tenant id to its slug and status."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_gateway_batch_ingest_effect.models.model_batch_ingest import (
    ModelTenantBinding,
)


@runtime_checkable
class ProtocolTenantBindings(Protocol):
    """Looks a tenant up by id only; the slug in a token is checked against the binding, never trusted."""

    def binding_for(self, tenant_id: str) -> ModelTenantBinding | None: ...


__all__ = ["ProtocolTenantBindings"]
