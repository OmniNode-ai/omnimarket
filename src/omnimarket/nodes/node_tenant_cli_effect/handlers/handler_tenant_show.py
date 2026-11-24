# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex tenant show``: publish the tenant read command and report its answer."""

from __future__ import annotations

from uuid import uuid4

from omnimarket.nodes.node_tenant_cli_effect.handlers import results
from omnimarket.nodes.node_tenant_cli_effect.handlers.contract_topics import (
    ContractUnavailableError,
    provision_topics,
)
from omnimarket.nodes.node_tenant_cli_effect.models.model_tenant_cli import (
    ModelTenantCliResult,
    ModelTenantShowRequest,
)
from omnimarket.nodes.node_tenant_cli_effect.ports.protocol_tenant_cli import (
    ProtocolTenantDispatch,
    TenantBusUnavailableError,
)

_COMMAND = "show"
_OPERATION = "tenant_read"


class HandlerTenantShow:
    """EFFECT: one read command on the bus, by tenant id or by slug."""

    def __init__(self, dispatch: ProtocolTenantDispatch) -> None:
        self._dispatch = dispatch

    def handle(self, request: ModelTenantShowRequest) -> ModelTenantCliResult:
        if (request.tenant_id is None) == (request.tenant_slug is None):
            return results.refusal(
                _COMMAND,
                "refused_invalid_request",
                "name exactly one of a tenant id or a tenant slug",
            )
        try:
            topics = provision_topics(_OPERATION)
        except ContractUnavailableError as exc:
            return results.node_problem(_COMMAND, "contract_unavailable", str(exc))
        payload: dict[str, object] = {"command_id": str(uuid4())}
        if request.tenant_id is not None:
            payload["tenant_id"] = str(request.tenant_id)
        else:
            payload["tenant_slug"] = request.tenant_slug
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


__all__: list[str] = ["HandlerTenantShow"]
