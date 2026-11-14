# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The source of the servable workflow contract catalog."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_workflow_contract_catalog_effect.models.model_workflow_contract_catalog import (
    ModelAdvertisedWorkflowContract,
)


class WorkflowCatalogError(RuntimeError):
    """The catalog is missing or invalid; nothing may be advertised."""


@runtime_checkable
class ProtocolWorkflowContractCatalog(Protocol):
    """Read the servable catalog, sorted by workflow type.

    Raises ``WorkflowCatalogError`` for a catalog that is absent, empty or
    invalid: the route answers unavailable and never an empty allowlist.
    """

    async def servable_contracts(
        self,
    ) -> tuple[ModelAdvertisedWorkflowContract, ...]: ...


__all__: list[str] = ["ProtocolWorkflowContractCatalog", "WorkflowCatalogError"]
