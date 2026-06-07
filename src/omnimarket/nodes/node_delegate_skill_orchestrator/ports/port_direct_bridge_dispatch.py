# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-process delegation dispatch port backed by the DelegationIntentBridge singleton.

Drives the full delegation chain (routing → inference → quality gate) without
any Kafka round-trips. Both HandlerDelegationWorkflow and DelegationIntentBridge
run in the same process, so this port resolves them from the DI container and
calls them directly.

This avoids the deadlock risk in the Kafka-backed RuntimeDelegationDispatchPort
when the delegation orchestrator and the delegate-skill handler share the same
runtime event loop: the Kafka port subscribes to delegation-completed, publishes
the request, then awaits asyncio.wait_for — which, if the consumer loop for
delegation-request is blocked inline on that same coroutine, would never deliver
the terminal event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel

from omnimarket.models.delegation.wire import (
    EnumQualityContractMode,
    ModelDelegationRequest,
    ModelDelegationResult,
    ModelQualityGateIntent,
)
from omnimarket.models.delegation.wire import (
    ModelDelegationEventEnvelope as ModelDelegationEvent,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
        HandlerDelegationWorkflow,
    )

    type DelegationIntentBridge = Any

_SUPPORTED_TASK_TYPES = frozenset(
    {
        "test",
        "document",
        "research",
        "code_generation",
        "refactor",
        "reasoning",
        "complex_reasoning",
        "planning",
        "review",
        "summarization",
        "agent_delegation",
        "escalation",
    }
)


def _coerce_task_type(task_type: str) -> str:
    """Pass through supported task types and normalize unknown values.

    Unknown types fall back to "research", which has the broadest quality gate
    criteria for legacy callers.
    """
    if task_type in _SUPPORTED_TASK_TYPES:
        return task_type
    return "research"


def _extract_delegation_result(
    events: list[BaseModel],
) -> ModelDelegationResult | None:
    for event in events:
        if isinstance(event, ModelDelegationEvent) and isinstance(
            event.payload, ModelDelegationResult
        ):
            return event.payload
        if isinstance(event, ModelDelegationResult):
            return event
    return None


def _result_to_dispatch_dict(
    result: ModelDelegationResult,
    correlation_id: UUID,
) -> dict[str, object]:
    return {
        "status": "completed" if result.quality_passed else "failed",
        "correlation_id": str(correlation_id),
        "content": result.content,
        "model_name": result.model_used,
        "delegated_to": result.endpoint_url,
        "quality_gate_passed": result.quality_passed,
        "quality_score": result.quality_score,
        "input_tokens": result.prompt_tokens,
        "output_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
        "delegation_latency_ms": result.latency_ms,
        "failure_reason": result.failure_reason,
        "tokens_to_compliance": result.tokens_to_compliance,
        "compliance_attempts": result.compliance_attempts,
    }


class DirectBridgeDelegationDispatchPort:
    """Drive the delegation chain in-process via the workflow FSM and intent bridge.

    This port accepts the two process-local singletons and calls them directly,
    avoiding any Kafka subscription or queue wait. The full chain runs
    synchronously within the awaited dispatch() call.
    """

    def __init__(
        self,
        *,
        workflow: HandlerDelegationWorkflow,
        bridge: DelegationIntentBridge,
    ) -> None:
        self._workflow = workflow
        self._bridge = bridge

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
        quality_contract_mode: str | EnumQualityContractMode,
        acceptance_criteria: tuple[str, ...],
    ) -> dict[str, object]:
        coerced_task_type = _coerce_task_type(task_type)
        request = ModelDelegationRequest(
            prompt=prompt,
            task_type=coerced_task_type,
            source_session_id=source_session_id,
            source_file_path=source_file_path,
            correlation_id=correlation_id,
            max_tokens=max_tokens,
            emitted_at=datetime.now(UTC),
            quality_contract_mode=quality_contract_mode,
            acceptance_criteria=acceptance_criteria,
        )

        if not wait:
            self._workflow.handle_delegation_request(request)
            return {
                "status": "submitted",
                "content": None,
                "delegated_to": "bridge",
                "model_name": None,
            }

        routing_intents = self._workflow.handle_delegation_request(request)
        if not routing_intents:
            return {
                "status": "failed",
                "error_message": "delegation workflow rejected request (duplicate or invalid correlation_id)",
            }

        for routing_intent in routing_intents:
            decision = await self._bridge.handle_routing_intent(routing_intent)
            inference_intents = self._workflow.handle_routing_decision(decision)

            for inference_intent in inference_intents:
                response = await self._bridge.handle_inference_intent(inference_intent)
                gate_intents_or_events = self._workflow.handle_inference_response(
                    response
                )

                while True:
                    next_intents: list[BaseModel] = []
                    terminal_events: list[BaseModel] = []
                    for item in gate_intents_or_events:
                        if isinstance(item, ModelQualityGateIntent):
                            next_intents.append(item)
                        else:
                            terminal_events.append(item)

                    if next_intents:
                        for gate_intent in next_intents:
                            gate_result = await self._bridge.handle_quality_gate_intent(
                                gate_intent
                            )
                            gate_intents_or_events = self._workflow.handle_gate_result(
                                gate_result
                            )
                        continue

                    result = _extract_delegation_result(terminal_events)
                    if result is not None:
                        return _result_to_dispatch_dict(result, correlation_id)

                    return {
                        "status": "failed",
                        "error_message": "delegation chain produced no terminal result",
                    }

        return {
            "status": "failed",
            "error_message": (
                "routing intents were processed for the delegation request but "
                "handle_routing_decision produced no completed inference path"
            ),
        }


__all__ = ["DirectBridgeDelegationDispatchPort"]
