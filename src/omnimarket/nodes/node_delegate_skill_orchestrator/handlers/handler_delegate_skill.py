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

from omnibase_core.models.delegation.wire import ModelPremiumCounterfactual

from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
    ModelDelegateSkillResponseMetrics,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
    ProtocolDelegationEventBus,
)
from omnimarket.pricing import (
    DEFAULT_BASELINE_MODEL,
    build_premium_counterfactual,
    estimate_baseline_cost_usd,
    estimate_frontier_costs_usd,
    get_manifest_version_int,
)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "timeout"})


class ProtocolDelegationDispatchPort(Protocol):
    """Injected port for delegation dispatch. Implementation is runtime-owned.

    OMN-13161: ``max_tokens`` is ``int | None``. ``None`` means the request
    omitted an explicit budget; the dispatch implementation resolves the effective
    value from the selected backend's per-backend ceiling in the routing contract.
    """

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: UUID,
        max_tokens: int | None,
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


def _estimate_claude_cost_savings(result: dict[str, object]) -> float:
    prompt_tokens = _as_int(result.get("input_tokens", result.get("prompt_tokens", 0)))
    completion_tokens = _as_int(
        result.get("output_tokens", result.get("completion_tokens", 0))
    )
    return round(
        estimate_baseline_cost_usd(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
        6,
    )


def _frontier_cost_estimates(result: dict[str, object]) -> dict[str, float]:
    prompt_tokens = _as_int(result.get("input_tokens", result.get("prompt_tokens", 0)))
    completion_tokens = _as_int(
        result.get("output_tokens", result.get("completion_tokens", 0))
    )
    return estimate_frontier_costs_usd(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def _premium_counterfactual(
    result: dict[str, object],
) -> ModelPremiumCounterfactual | None:
    """Build the pinned premium counterfactual from measured tokens (OMN-13355)."""
    prompt_tokens = _as_int(result.get("input_tokens", result.get("prompt_tokens", 0)))
    completion_tokens = _as_int(
        result.get("output_tokens", result.get("completion_tokens", 0))
    )
    premium_model = str(
        result.get("model_cloud_baseline")
        or result.get("baseline_model")
        or DEFAULT_BASELINE_MODEL
    )
    return build_premium_counterfactual(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        premium_model=premium_model,
    )


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
        model_cloud_baseline=str(
            result.get("model_cloud_baseline")
            or result.get("baseline_model")
            or DEFAULT_BASELINE_MODEL
        ),
        pricing_manifest_version=_as_int(
            result.get("pricing_manifest_version"),
            default=get_manifest_version_int(),
        ),
        prompt_text=request.prompt,
        response=str(result.get("content", "")),
        quality_gate_passed=bool(
            result.get("quality_gate_passed", result.get("quality_passed", False))
        ),
        quality_score=_as_float(result.get("quality_score")),
        quality_gates_failed=quality_failures,
        error_message=error_message,
        metrics=ModelDelegateSkillResponseMetrics(
            input_tokens=_as_int(
                result.get("input_tokens", result.get("prompt_tokens", 0))
            ),
            output_tokens=_as_int(
                result.get("output_tokens", result.get("completion_tokens", 0))
            ),
            total_tokens=_as_int(result.get("total_tokens")),
            tokens_to_compliance=_as_int(result.get("tokens_to_compliance")),
            compliance_attempts=_as_int(result.get("compliance_attempts")),
            cost_usd=_as_float(result.get("cost_usd")),
            cost_savings_usd=_as_float(
                result.get("cost_savings_usd"),
                default=_estimate_claude_cost_savings(result),
            ),
            frontier_costs_usd=_frontier_cost_estimates(result),
            premium_counterfactual=_premium_counterfactual(result),
            latency_ms=_as_int(
                result.get("delegation_latency_ms", result.get("latency_ms", 0))
            ),
        ),
    )


class HandlerDelegateSkill:
    """Translate a typed delegation request to a runtime command via the port."""

    def __init__(
        self,
        event_bus: ProtocolDelegationEventBus | None = None,
        *,
        dispatch_port: ProtocolDelegationDispatchPort | None = None,
    ) -> None:
        if dispatch_port is not None:
            self._dispatch_port: ProtocolDelegationDispatchPort = dispatch_port
        else:
            # Transport-aware port selection is owned by the ports package, not
            # this domain handler. A bus-less or in-memory single-process runtime
            # resolves to the in-process local port (routing + canonical effect +
            # quality gate + sqlite evidence row, OMN-13160/OMN-13601); an external
            # broker bus resolves to the runtime publish/await port. Imported
            # lazily to avoid a construction-time import cycle with the ports
            # package, which references this handler's port protocol.
            from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_selection import (
                select_delegation_dispatch_port,
            )

            self._dispatch_port = select_delegation_dispatch_port(event_bus)

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
                source_file_path=request.source_file_path,
                source_session_id=request.session_id
                or request.metadata.get("session_id"),
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
