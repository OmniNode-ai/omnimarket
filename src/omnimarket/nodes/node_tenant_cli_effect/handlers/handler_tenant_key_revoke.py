# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex tenant key revoke``: publish the gateway key revoke command and report its answer."""

from __future__ import annotations

from omnimarket.nodes.node_tenant_cli_effect.handlers import results
from omnimarket.nodes.node_tenant_cli_effect.handlers.contract_topics import (
    ContractUnavailableError,
    key_topics,
)
from omnimarket.nodes.node_tenant_cli_effect.models.model_tenant_cli import (
    ModelTenantCliResult,
    ModelTenantKeyRevokeRequest,
)
from omnimarket.nodes.node_tenant_cli_effect.ports.protocol_tenant_cli import (
    ProtocolTenantDispatch,
    TenantBusUnavailableError,
)

_COMMAND = "key revoke"
OPERATION = "tenant_api_key_revoke_requested"
ANSWER_EVENT = "ModelTenantApiKeyRevokeResult"


class HandlerTenantKeyRevoke:
    """EFFECT: one revoke command on the bus, for one credential row of one tenant."""

    def __init__(self, dispatch: ProtocolTenantDispatch) -> None:
        self._dispatch = dispatch

    def handle(self, request: ModelTenantKeyRevokeRequest) -> ModelTenantCliResult:
        try:
            topics = key_topics(OPERATION, ANSWER_EVENT)
        except ContractUnavailableError as exc:
            return results.node_problem(_COMMAND, "contract_unavailable", str(exc))
        # The node's command carries a required correlation_id, which the bus caller sets to the envelope's own.
        payload: dict[str, object] = {
            "tenant_id": str(request.tenant_id),
            "credential_id": str(request.credential_id),
        }
        try:
            answer = self._dispatch.dispatch(
                node_id=topics.node_id,
                command_topic=topics.requested,
                response_topic=topics.completed,
                payload=payload,
                correlation_in_payload=True,
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


__all__: list[str] = ["HandlerTenantKeyRevoke"]
