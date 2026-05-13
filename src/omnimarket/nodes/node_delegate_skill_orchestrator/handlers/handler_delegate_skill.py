# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation skill handler — domain translator with an injected dispatch port.

This handler translates consumer-facing delegation requests into runtime-internal
delegation commands. It owns no transport detail: the dispatch port it receives at
construction is runtime-owned and resolved through dependency injection. The
handler never names a wire address, a broker, a message bus subject, or any adapter
internal.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
    ModelDelegateSkillResponseMetrics,
)


class ProtocolDelegationDispatchPort(Protocol):
    """Injected port for delegation dispatch. Implementation is runtime-owned."""

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: UUID,
        max_tokens: int,
        source_file_path: str | None,
        source_session_id: str | None,
        wait: bool,
    ) -> dict[str, object]: ...


class HandlerDelegateSkill:
    """Translate a typed delegation request to a runtime command via the port."""

    def __init__(self, *, dispatch_port: ProtocolDelegationDispatchPort) -> None:
        self._dispatch_port = dispatch_port

    async def handle(
        self, request: ModelDelegateSkillRequest
    ) -> ModelDelegateSkillResponse:
        """Dispatch the request and return a typed response.

        On any dispatch exception, returns ``status="failed"`` with the error text
        rather than propagating — failed delegations must remain observable.
        """
        try:
            result = await self._dispatch_port.dispatch(
                prompt=request.prompt,
                task_type=request.task_type,
                correlation_id=request.correlation_id,
                max_tokens=request.max_tokens,
                source_file_path=request.cwd,
                source_session_id=request.metadata.get("session_id"),
                wait=request.wait,
            )
        except Exception as exc:
            return ModelDelegateSkillResponse(
                status="failed",
                correlation_id=request.correlation_id,
                task_type=request.task_type,
                error_message=str(exc),
            )

        status_value = str(result.get("status", "completed"))
        if status_value not in {"completed", "failed", "timeout"}:
            status_value = "completed"

        return ModelDelegateSkillResponse(
            status=status_value,  # type: ignore[arg-type]
            correlation_id=request.correlation_id,
            task_type=request.task_type,
            provider=str(result.get("delegated_to", "")),
            model_name=str(result.get("model_name", "")),
            response=str(result.get("content", "")),
            quality_gate_passed=bool(result.get("quality_gate_passed", False)),
            metrics=ModelDelegateSkillResponseMetrics(
                input_tokens=int(result.get("input_tokens", 0) or 0),
                output_tokens=int(result.get("output_tokens", 0) or 0),
                cost_usd=float(result.get("cost_usd", 0.0) or 0.0),
                cost_savings_usd=float(result.get("cost_savings_usd", 0.0) or 0.0),
                latency_ms=int(result.get("delegation_latency_ms", 0) or 0),
            ),
        )
