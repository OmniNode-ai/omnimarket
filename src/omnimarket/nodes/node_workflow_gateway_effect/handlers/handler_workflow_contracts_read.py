# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contracts read handler: advertise the servable workflow catalog before a client submits.

Exemplar: handler_workflow_status_read.py in this node (injected dependency, typed answer).
"""

from __future__ import annotations

from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_catalog import (
    WorkflowCatalog,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_http import (
    ModelWorkflowContractsCommand,
    ModelWorkflowHttpResponse,
)


class HandlerWorkflowContractsRead:
    """Return the servable catalog entries and their payload schemas, verbatim.

    Fenced entries are omitted: advertising a type the gateway refuses would be a lie.
    The schema is the catalog's own, never a restatement, so a client and the gateway
    read one source.
    """

    def __init__(self, *, catalog: WorkflowCatalog) -> None:
        self._catalog = catalog

    async def handle(
        self, request: ModelWorkflowContractsCommand
    ) -> ModelWorkflowHttpResponse:
        del request
        contracts = [
            {
                "workflow_type": entry.workflow_type,
                "contract_id": entry.contract_id,
                "payload_schema": entry.payload_schema,
            }
            for entry in self._catalog.contracts()
        ]
        return ModelWorkflowHttpResponse(status_code=200, body={"contracts": contracts})


__all__: list[str] = ["HandlerWorkflowContractsRead"]
