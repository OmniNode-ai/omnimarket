# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Status read handler: the tenant-scoped read model of one workflow."""

from __future__ import annotations

import logging
from uuid import UUID

from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_receipt_render import (
    decode_rule_evaluations,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.enum_workflow_refusal_code import (
    EnumWorkflowRefusalCode,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_envelope import (
    ModelWorkflowStatus,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_http import (
    ModelWorkflowHttpResponse,
    ModelWorkflowStatusCommand,
)
from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_store import (
    ProtocolWorkflowStore,
    WorkflowStoreUnavailableError,
)

logger = logging.getLogger(__name__)


class HandlerWorkflowStatusRead:
    """Reads the status of one workflow by id."""

    def __init__(self, *, store: ProtocolWorkflowStore) -> None:
        self._store = store

    async def handle(
        self, request: ModelWorkflowStatusCommand
    ) -> ModelWorkflowHttpResponse:
        try:
            workflow_id = UUID(request.workflow_id)
        except (ValueError, TypeError):
            return ModelWorkflowHttpResponse(
                status_code=400, body={"detail": "workflow_id must be a UUID"}
            )

        try:
            row = await self._store.fetch(
                workflow_id=workflow_id, tenant_id=request.caller.tenant_id
            )
        except WorkflowStoreUnavailableError:
            return ModelWorkflowHttpResponse(
                status_code=503, body={"detail": "workflow status is unavailable"}
            )

        if row is None:
            return ModelWorkflowHttpResponse(
                status_code=404,
                body={
                    "detail": "workflow not found",
                    "code": EnumWorkflowRefusalCode.RESOURCE_NOT_FOUND.value,
                },
            )

        status = ModelWorkflowStatus(
            workflow_id=row.workflow_id,
            workflow_type=row.workflow_type,
            status=row.status,
            envelope_id=row.envelope_id,
            correlation_id=row.correlation_id,
            command_topic=row.command_topic,
            submitted_at=row.submitted_at,
            updated_at=row.updated_at,
            terminal_model_used=row.terminal_model_used,
            terminal_total_tokens=row.terminal_total_tokens,
            terminal_latency_ms=row.terminal_latency_ms,
            terminal_failure_class=row.terminal_failure_class,
            terminal_failure_code=row.terminal_failure_code,
            terminal_failure_reason=row.terminal_failure_reason,
            terminal_remediation=row.terminal_remediation,
            rule_evaluations=decode_rule_evaluations(row.terminal_rule_evaluations),
        )
        return ModelWorkflowHttpResponse(
            status_code=200, body=status.model_dump(mode="json")
        )


__all__: list[str] = ["HandlerWorkflowStatusRead"]
