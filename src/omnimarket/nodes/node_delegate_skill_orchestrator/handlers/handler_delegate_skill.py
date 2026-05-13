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

from typing import Literal, Protocol
from uuid import UUID

from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
    ModelDelegateSkillResponseMetrics,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
    ProtocolDelegationEventBus,
    RuntimeDelegationDispatchPort,
)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "timeout"})


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
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
    ) -> dict[str, object]: ...


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(item) for item in value]
    if isinstance(value, str) and value:
        return [value]
    return []


def _response_from_result(
    request: ModelDelegateSkillRequest, result: dict[str, object]
) -> ModelDelegateSkillResponse:
    raw_status = str(result.get("status", "completed"))
    is_known_status = raw_status in _TERMINAL_STATUSES
    status_value: Literal["completed", "failed", "timeout"] = (
        raw_status  # type: ignore[assignment]
        if is_known_status
        else "failed"
    )
    error_message = str(
        result.get("error_message") or result.get("failure_reason") or ""
    )
    if not is_known_status:
        error_message = f"runtime returned unknown terminal status {raw_status!r}"
    quality_failures = _as_str_list(
        result.get("quality_gates_failed", result.get("failure_reason", ""))
    )
    return ModelDelegateSkillResponse(
        status=status_value,
        correlation_id=request.correlation_id,
        task_type=request.task_type,
        provider=str(result.get("delegated_to") or result.get("endpoint_url") or ""),
        model_name=str(result.get("model_name") or result.get("model_used") or ""),
        response=str(result.get("content", "")),
        quality_gate_passed=bool(
            result.get("quality_gate_passed", result.get("quality_passed", False))
        ),
        quality_gates_failed=quality_failures,
        error_message=error_message,
        metrics=ModelDelegateSkillResponseMetrics(
            input_tokens=_as_int(
                result.get("input_tokens", result.get("prompt_tokens", 0))
            ),
            output_tokens=_as_int(
                result.get("output_tokens", result.get("completion_tokens", 0))
            ),
            cost_usd=_as_float(result.get("cost_usd")),
            cost_savings_usd=_as_float(result.get("cost_savings_usd")),
            latency_ms=_as_int(
                result.get("delegation_latency_ms", result.get("latency_ms", 0))
            ),
        ),
    )


class HandlerDelegateSkill:
    """Translate a typed delegation request to a runtime command via the port."""

    def __init__(
        self,
        event_bus: ProtocolDelegationEventBus,
        *,
        dispatch_port: ProtocolDelegationDispatchPort | None = None,
    ) -> None:
        self._dispatch_port = dispatch_port or RuntimeDelegationDispatchPort(
            event_bus=event_bus
        )

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
                quality_contract_mode=request.quality_contract_mode,
                acceptance_criteria=request.acceptance_criteria,
            )
        except Exception as exc:
            return ModelDelegateSkillResponse(
                status="failed",
                correlation_id=request.correlation_id,
                task_type=request.task_type,
                error_message=str(exc),
            )

        return _response_from_result(request, result)
