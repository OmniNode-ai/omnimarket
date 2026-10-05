# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recover an outer delegation when its inner terminal outlives the runtime."""

from datetime import UTC, datetime

from omnibase_core.models.delegation.wire import (
    ModelDelegationFailed,
    ModelDelegationResult,
)

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _response_from_result,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    delegate_skill_terminal_from_response,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegation_reap_context import (
    DELEGATION_RUNTIME_INSTANCE_ID,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_delegation_claim import (
    ProtocolDelegationRecoveryPort,
    resolve_delegation_claim_store,
)


class HandlerDelegationRecovery:
    """A permanent subscription replaces the waiter a restart destroyed."""

    def __init__(self, *, port: ProtocolDelegationRecoveryPort | None = None) -> None:
        self._port = port

    async def handle(
        self, result: ModelDelegationResult
    ) -> ModelDelegateSkillCompleted | ModelDelegateSkillFailed | None:
        if self._port is None:
            self._port = resolve_delegation_claim_store()
        claims = self._port.pending_claims(correlation_id=result.correlation_id)
        # Correlation is a retry identity, not a delivery identity. An ambiguous
        # join cannot assign one inner result to multiple outer commands.
        if len(claims) != 1:
            return None
        claim = claims[0]
        ctx = claim.context
        now = datetime.now(UTC)
        if (
            ctx.request is None
            or ctx.runtime_instance_id is None
            or ctx.runtime_instance_id == DELEGATION_RUNTIME_INSTANCE_ID
            or now >= ctx.deadline_at
        ):
            return None
        raw = result.model_dump(mode="python")
        raw["status"] = (
            "failed"
            if isinstance(result, ModelDelegationFailed) or result.failure_reason
            else "completed"
        )
        terminal = delegate_skill_terminal_from_response(
            _response_from_result(
                ctx.request,
                raw,
                tenant_id=ctx.tenant_id,
                queue_wait_ms=None,
                execution_duration_ms=max(
                    0, int((now - claim.claimed_at).total_seconds() * 1000)
                ),
                budget_evidence=result.budget_evidence,
            )
        ).model_copy(
            update={
                "command_id": claim.delivery_id,
                "ticket_id": ctx.ticket_id,
                "caller_lane": ctx.caller_lane,
                "session_id": ctx.session_id,
            }
        )
        outcome = self._port.record_terminal(
            delivery_id=claim.delivery_id,
            terminal={
                "cls": type(terminal).__name__,
                "data": terminal.model_dump(mode="json"),
            },
        )
        return terminal if outcome.won else None
