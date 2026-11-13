# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipt read handler: the terminal receipt of one workflow."""

from __future__ import annotations

import logging
from uuid import UUID

from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_receipt_render import (
    WorkflowReceiptNotTerminalError,
    render_workflow_receipt,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.enum_workflow_refusal_code import (
    EnumWorkflowRefusalCode,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_http import (
    ModelWorkflowHttpResponse,
    ModelWorkflowReceiptCommand,
)
from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_store import (
    ProtocolWorkflowStore,
    WorkflowStoreUnavailableError,
)

logger = logging.getLogger(__name__)


class HandlerWorkflowReceiptRead:
    """Reads the terminal receipt of one workflow by id."""

    def __init__(self, *, store: ProtocolWorkflowStore) -> None:
        self._store = store

    async def handle(
        self, request: ModelWorkflowReceiptCommand
    ) -> ModelWorkflowHttpResponse:
        try:
            workflow_id = UUID(request.workflow_id)
        except (ValueError, TypeError):
            return ModelWorkflowHttpResponse(
                status_code=400, body={"detail": "workflow_id must be a UUID"}
            )

        runner_identity = request.runner_identity
        if runner_identity is None or not runner_identity.strip():
            return ModelWorkflowHttpResponse(
                status_code=400,
                body={"detail": "runner_identity is required and must be non-empty"},
            )

        try:
            row = await self._store.fetch(
                workflow_id=workflow_id, tenant_id=request.caller.tenant_id
            )
        except WorkflowStoreUnavailableError:
            return ModelWorkflowHttpResponse(
                status_code=503, body={"detail": "workflow receipt is unavailable"}
            )

        if row is None:
            return ModelWorkflowHttpResponse(
                status_code=404,
                body={
                    "detail": "workflow not found",
                    "code": EnumWorkflowRefusalCode.RESOURCE_NOT_FOUND.value,
                },
            )

        try:
            receipt = render_workflow_receipt(row, runner_identity)
        except WorkflowReceiptNotTerminalError as exc:
            return ModelWorkflowHttpResponse(status_code=409, body={"detail": str(exc)})

        return ModelWorkflowHttpResponse(
            status_code=200, body=receipt.model_dump(mode="json")
        )


__all__: list[str] = ["HandlerWorkflowReceiptRead"]
