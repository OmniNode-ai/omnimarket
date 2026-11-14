# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared route configuration and the dependencies the mount binds."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.container.container_overlay_dependencies import (
    installed_overlay_document,
)
from omnimarket.nodes.node_workflow_contract_catalog_effect.handlers.overlay_catalog import (
    OverlayWorkflowContractCatalog,
)
from omnimarket.nodes.node_workflow_contract_catalog_effect.models.model_workflow_contract_catalog import (
    ModelCatalogHttpRoute,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

_identity: list[ProtocolHttpTenantIdentity] = []


def load_http_route() -> ModelCatalogHttpRoute:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    mount = contract["http_mount"]
    route = ModelCatalogHttpRoute.model_validate(contract["http_route"])
    if mount["path_prefix"] != route.path:
        raise ValueError("the mount prefix must equal the route path")
    return route


def bind_tenant_identity(resolver: ProtocolHttpTenantIdentity | None) -> None:
    """Bind the hosted edge's caller resolver, or remove it with ``None``.

    With none bound the mount refuses every request.
    """
    _identity.clear()
    if resolver is not None:
        _identity.append(resolver)


def bound_tenant_identity() -> ProtocolHttpTenantIdentity | None:
    return _identity[0] if _identity else None


class _InstalledOverlayDocuments:
    """Reads a named document from the lane overlay installed in this process."""

    def read(self, name: str) -> dict[str, Any] | None:
        return installed_overlay_document(name)


def production_catalog(route: ModelCatalogHttpRoute) -> OverlayWorkflowContractCatalog:
    return OverlayWorkflowContractCatalog(
        documents=_InstalledOverlayDocuments(),
        document_name=route.catalog_document,
        command_topics=route.command_topics,
    )
