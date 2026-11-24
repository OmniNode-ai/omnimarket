# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex tenant create``: publish the tenant provision command and report its answer."""

from __future__ import annotations

from uuid import uuid4

from omnimarket.nodes.node_tenant_cli_effect.handlers import results
from omnimarket.nodes.node_tenant_cli_effect.handlers.contract_topics import (
    ContractUnavailableError,
    provision_topics,
)
from omnimarket.nodes.node_tenant_cli_effect.models.model_tenant_cli import (
    ModelTenantCliResult,
    ModelTenantCreateRequest,
)
from omnimarket.nodes.node_tenant_cli_effect.ports.protocol_tenant_cli import (
    ProtocolTenantDispatch,
    TenantBusUnavailableError,
)

_COMMAND = "create"
_OPERATION = "tenant_provision"


class HandlerTenantCreate:
    """EFFECT: one provision command on the bus; the node's typed outcome is the result."""

    def __init__(self, dispatch: ProtocolTenantDispatch) -> None:
        self._dispatch = dispatch

    def handle(self, request: ModelTenantCreateRequest) -> ModelTenantCliResult:
        try:
            topics = provision_topics(_OPERATION)
        except ContractUnavailableError as exc:
            return results.node_problem(_COMMAND, "contract_unavailable", str(exc))
        # command_id is this command's id and the node's replay key; the node reads no correlation id from the payload.
        payload: dict[str, object] = {
            "command_id": str(uuid4()),
            "tenant_slug": request.tenant_slug,
        }
        if request.name is not None:
            payload["name"] = request.name
        try:
            answer = self._dispatch.dispatch(
                node_id=topics.node_id,
                command_topic=topics.requested,
                response_topic=topics.completed,
                payload=payload,
                correlation_in_payload=False,
            )
        except TenantBusUnavailableError as exc:
            return results.node_problem(_COMMAND, "bus_unavailable", str(exc))
        if answer is None:
            return results.node_problem(
                _COMMAND,
                "no_answer",
                f"no answer on {topics.completed}; is a runtime serving {topics.requested}?",
            )
        return results.from_answer(_COMMAND, answer)


__all__: list[str] = ["HandlerTenantCreate"]
