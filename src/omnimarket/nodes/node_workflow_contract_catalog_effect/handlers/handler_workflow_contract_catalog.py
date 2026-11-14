# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer one catalog read: resolve the caller first, read the catalog only for a tenant."""

from __future__ import annotations

from omnimarket.nodes.node_workflow_contract_catalog_effect.models.model_workflow_contract_catalog import (
    ModelCatalogHttpRoute,
    ModelCatalogRefusal,
    ModelCatalogRequest,
    ModelCatalogResponse,
)
from omnimarket.nodes.node_workflow_contract_catalog_effect.protocols.protocol_workflow_contract_catalog import (
    ProtocolWorkflowContractCatalog,
    WorkflowCatalogError,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)


class HandlerWorkflowContractCatalog:
    """Advertise the servable workflow contracts to an authenticated tenant."""

    def __init__(
        self,
        *,
        identity: ProtocolHttpTenantIdentity | None,
        catalog: ProtocolWorkflowContractCatalog,
        config: ModelCatalogHttpRoute,
    ) -> None:
        self._identity = identity
        self._catalog = catalog
        self._config = config

    async def handle(self, request: ModelCatalogRequest) -> ModelCatalogResponse:
        # Order is the contract: caller first, catalog read last. A request that
        # is refused never touches the catalog.
        if self._identity is None:
            return self._refuse(self._config.refusals["resolver_unbound"])
        tenant = await self._identity.resolve(request.headers)
        if tenant is None:
            presented = any(
                request.headers.get(name, "") != ""
                for name in self._config.credential_headers
            )
            key = "credential_invalid" if presented else "credential_absent"
            return self._refuse(self._config.refusals[key])
        try:
            contracts = await self._catalog.servable_contracts()
        except WorkflowCatalogError:
            return self._refuse(self._config.refusals["catalog_unavailable"])
        return ModelCatalogResponse(
            status_code=200,
            body={"contracts": [c.model_dump(mode="json") for c in contracts]},
        )

    @staticmethod
    def _refuse(refusal: ModelCatalogRefusal) -> ModelCatalogResponse:
        body = {"detail": refusal.detail}
        if refusal.code is not None:
            body["code"] = refusal.code
        return ModelCatalogResponse(status_code=refusal.status_code, body=body)


__all__: list[str] = ["HandlerWorkflowContractCatalog"]
