# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Producer of the harness-execute command: one pinned delegation to a ``kind: harness`` backend (OMN-26220).

``node_llm_delegation_call_effect`` consumes the harness-execute topic with
``HandlerHarnessDelegation`` (OMN-26219). This is the producer the topic lacked: the runtime
dispatch port hands it a delegation whose ``backend_id`` the routing overlay declares as a
harness backend, it publishes the typed ``ModelHarnessBackendCallRequest`` and reads the
handler's ``ModelHarnessBackendCallResult`` back as the delegation terminal. The topics come
from the node contract; which backends are harness backends is an overlay fact.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from uuid import UUID

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.harness_backend_call import (
    ModelHarnessAttemptRecord,
    ModelHarnessBackendCallRequest,
    ModelHarnessBackendCallResult,
)
from omnimarket.models.delegation.delegation_caller_lane import (
    DELEGATION_CALLER_LANE_METADATA_KEY,
)
from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendKind,
    EnumDelegationBackendSurface,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.dispatch_progress import (
    dispatch_stage,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models import (
    ModelRuntimeDelegationDispatchConfig,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.protocol_delegation_event_bus import (
    ProtocolDelegationEventBus,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG

_REQUEST_MESSAGE_TYPE = "omnimarket.delegation-harness-execute"


def _message_value(message: object) -> bytes | str | None:
    raw = getattr(message, "value", None)
    if isinstance(raw, bytearray):
        return bytes(raw)
    if isinstance(raw, bytes | str):
        return raw
    return None


def _parse_result(
    value: bytes | str, *, request_id: UUID
) -> ModelHarnessBackendCallResult | None:
    """The handler's result for ``request_id``, or None for any other record."""
    try:
        raw = json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    payload = raw.get("payload", raw)
    if not isinstance(payload, dict) or payload.get("request_id") != str(request_id):
        return None
    try:
        return ModelHarnessBackendCallResult.model_validate(payload)
    except ValueError:
        return None


def _history_row(attempt: ModelHarnessAttemptRecord) -> dict[str, object]:
    return {
        "tier": attempt.tier,
        "backend_id": attempt.backend_id,
        "model_used": attempt.model_name or "",
        "acceptance_decision": "accept" if attempt.accepted else "climb",
        "cost_usd": float(attempt.cost_usd) if attempt.cost_usd is not None else 0.0,
        "usage_source": "measured" if attempt.cost_usd is not None else "unknown",
        "failure_reasons": list(attempt.gate_reasons),
        "tokens_in": attempt.tokens_in,
        "tokens_out": attempt.tokens_out,
    }


def terminal_from_result(result: ModelHarnessBackendCallResult) -> dict[str, object]:
    """The runtime port's terminal dict for one harness call, with a cost row per attempt."""
    answer: dict[str, object] = {
        "status": "completed" if result.ok else "failed",
        "correlation_id": str(result.request_id),
        "content": result.content,
        "model_name": result.model_name or "",
        "provider": result.backend_id or "",
        "quality_gate_passed": result.ok,
        "escalation_history": [_history_row(a) for a in result.attempts],
        "attempts_count": len(result.attempts),
    }
    if not result.ok:
        refusals = "; ".join(
            f"{r.tier}: {', '.join(x.value for x in r.refusals)}"
            for r in result.refused_rungs
        )
        answer["error_message"] = "; ".join(
            part
            for part in (f"harness call {result.status}", result.detail, refusals)
            if part
        )
    return answer


class HarnessDelegationDispatch:
    """Publishes one harness-execute command and awaits the handler's result."""

    def __init__(
        self,
        *,
        event_bus: ProtocolDelegationEventBus,
        config: ModelRuntimeDelegationDispatchConfig,
        overlay: ModelDelegationRoutingOverlay,
    ) -> None:
        topics = config.topics
        if topics.harness_command is None or topics.harness_completed is None:
            raise ValueError(
                "the harness-execute topics are not declared in the node contract"
            )
        self._bus = event_bus
        self._config = config
        self._command_topic = topics.harness_command
        self._completed_topic = topics.harness_completed
        self._overlay = overlay

    def admits(self, backend_id: str | None) -> bool:
        """True when the overlay declares ``backend_id`` as a harness backend."""
        if backend_id is None:
            return False
        backend = self._overlay.backend_by_id().get(backend_id)
        return backend is not None and backend.kind is EnumDelegationBackendKind.HARNESS

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: UUID,
        wait: bool,
        execution_timeout_seconds: int,
        tenant_id: str | None,
        backend_id: str,
        attribution: Mapping[str, str] | None,
    ) -> dict[str, object]:
        request = ModelHarnessBackendCallRequest(
            request_id=correlation_id,
            task_class=task_type,
            prompt=prompt,
            tenant_slug=tenant_id if tenant_id is not None else HOUSE_TENANT_SLUG,
            surface=EnumDelegationBackendSurface.INTERNAL,
            pinned_backend_id=backend_id,
            timeout_seconds=float(execution_timeout_seconds),
            caller_lane=(attribution or {}).get(DELEGATION_CALLER_LANE_METADATA_KEY),
        )
        if not wait:
            with dispatch_stage("publish"):
                await self._publish(request)
            return {
                "status": "completed",
                "content": "",
                "delegated_to": "harness",
                "model_name": "",
                "quality_gate_passed": False,
            }
        queue: asyncio.Queue[ModelHarnessBackendCallResult] = asyncio.Queue()

        async def on_message(message: object) -> None:
            value = _message_value(message)
            if value is None:
                return
            result = _parse_result(value, request_id=correlation_id)
            if result is not None:
                await queue.put(result)

        with dispatch_stage("subscribe"):
            unsubscribe: Callable[[], Awaitable[None]] = await self._bus.subscribe(
                self._completed_topic,
                None,
                on_message,
                group_id=f"{self._config.consumer_group_prefix}-harness-{correlation_id.hex}",
            )
        stage = "publish"
        try:
            with dispatch_stage("publish"):
                await self._publish(request)
            stage = "terminal_wait"
            with dispatch_stage("terminal_wait"):
                result = await asyncio.wait_for(
                    queue.get(), timeout=float(self._config.wait_timeout_seconds)
                )
        except TimeoutError as exc:
            return {
                "status": "timeout",
                "error_message": (
                    f"harness delegation timed out at stage={stage}"
                    f" waiting for its result: {exc}"
                ),
            }
        finally:
            with dispatch_stage("terminal_cleanup"):
                await unsubscribe()
        return terminal_from_result(result)

    async def _publish(self, request: ModelHarnessBackendCallRequest) -> None:
        envelope = ModelEventEnvelope[ModelHarnessBackendCallRequest](
            payload=request,
            correlation_id=request.request_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=_REQUEST_MESSAGE_TYPE,
            source_tool=self._config.source_tool,
        )
        await self._bus.publish(
            self._command_topic,
            None,
            envelope.model_dump_json(exclude_none=True).encode("utf-8"),
            None,
        )


__all__ = ["HarnessDelegationDispatch", "terminal_from_result"]
