# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical dispatcher for the delegation workflow handler."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4, uuid5

from omnibase_core.enums import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.enums import (
    EnumDispatchStatus,
    EnumInfraTransportType,
    EnumMessageCategory,
)
from omnibase_infra.errors import InfraUnavailableError
from omnibase_infra.mixins import MixinAsyncCircuitBreaker
from omnibase_infra.models.dispatch.model_dispatch_result import ModelDispatchResult
from omnibase_infra.nodes.node_registration_orchestrator.dispatchers._util_envelope_extract import (
    extract_envelope_fields,
)
from omnibase_infra.utils import sanitize_error_message
from pydantic import BaseModel, ValidationError

from omnimarket.nodes.node_delegation_orchestrator.dispatchers.topic_utils import (
    derive_event_type_from_topic,
)

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus import ProtocolEventBus

    from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
        HandlerDelegationWorkflow,
    )

__all__ = ["DispatcherDelegationWorkflow"]

logger = logging.getLogger(__name__)

TOPIC_ID_DELEGATION_WORKFLOW = "delegation.workflow"


class DispatcherDelegationWorkflow(MixinAsyncCircuitBreaker):  # type: ignore[misc]
    """Dispatcher that delegates payload authority to HandlerDelegationWorkflow."""

    def __init__(
        self,
        handler: HandlerDelegationWorkflow,
        event_bus: ProtocolEventBus | None = None,
    ) -> None:
        self._handler = handler
        self._event_bus = event_bus
        self._init_circuit_breaker(
            threshold=3,
            reset_timeout=20.0,
            service_name="dispatcher.delegation.workflow",
            transport_type=EnumInfraTransportType.KAFKA,
        )

    @property
    def dispatcher_id(self) -> str:
        return "dispatcher.delegation.workflow"

    @property
    def category(self) -> EnumMessageCategory:
        return EnumMessageCategory.EVENT

    @property
    def message_types(self) -> set[str]:
        return {
            "ModelAgentTaskLifecycleEvent",
            "ModelDelegationRequest",
            "ModelInferenceResponseData",
            "ModelInvocationCommand",
            "ModelQualityGateResult",
            "ModelRoutingDecision",
            "omnibase-infra.agent-task-lifecycle",
            "omnibase-infra.delegation-request",
            "omnibase-infra.inference-response",
            "omnibase-infra.invocation",
            "omnibase-infra.quality-gate-result",
            "omnibase-infra.routing-decision",
        }

    @property
    def node_kind(self) -> EnumNodeKind:
        return EnumNodeKind.ORCHESTRATOR

    async def _publish_events_direct(
        self,
        events: list[BaseModel],
        correlation_id: UUID,
    ) -> list[BaseModel]:
        """Publish topic-bearing events directly when the bus is wired."""
        if self._event_bus is None:
            return events

        unpublished: list[BaseModel] = []
        for idx, event in enumerate(events):
            topic = getattr(event, "topic", None)
            if topic is None:
                unpublished.append(event)
                continue
            envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
                envelope_id=uuid5(correlation_id, f"{type(event).__name__}:{idx}"),
                payload=event,
                correlation_id=correlation_id,
                event_type=derive_event_type_from_topic(topic),
                envelope_timestamp=datetime.now(UTC),
            )
            await self._event_bus.publish_envelope(
                envelope=envelope,  # type: ignore[arg-type]
                topic=topic,
            )
            logger.info(
                "DispatcherDelegationWorkflow published %s to %s (correlation_id=%s)",
                type(event).__name__,
                topic,
                str(correlation_id),
            )
        return unpublished

    async def handle(self, envelope: object) -> ModelDispatchResult:
        started_at = datetime.now(UTC)
        correlation_id = uuid4()

        try:
            if not isinstance(envelope, (dict, ModelEventEnvelope)):
                return ModelDispatchResult(
                    dispatch_id=uuid4(),
                    status=EnumDispatchStatus.INVALID_MESSAGE,
                    topic=TOPIC_ID_DELEGATION_WORKFLOW,
                    dispatcher_id=self.dispatcher_id,
                    started_at=started_at,
                    completed_at=started_at,
                    duration_ms=0.0,
                    error_message=f"Unsupported envelope type: {type(envelope).__name__}",
                    correlation_id=correlation_id,
                    output_events=[],
                )

            extracted_correlation_id, raw_payload = extract_envelope_fields(
                cast("ModelEventEnvelope[object] | dict[str, object]", envelope)
            )
            correlation_id = extracted_correlation_id or correlation_id

            async with self._circuit_breaker_lock:
                await self._check_circuit_breaker("handle", correlation_id)

            events = await self._handler.handle(raw_payload)
            unpublished = await self._publish_events_direct(events, correlation_id)

            completed_at = datetime.now(UTC)
            duration_ms = (completed_at - started_at).total_seconds() * 1000

            async with self._circuit_breaker_lock:
                await self._reset_circuit_breaker()

            return ModelDispatchResult(
                dispatch_id=uuid4(),
                status=EnumDispatchStatus.SUCCESS,
                topic=TOPIC_ID_DELEGATION_WORKFLOW,
                dispatcher_id=self.dispatcher_id,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
                output_events=unpublished,
            )

        except InfraUnavailableError as e:
            completed_at = datetime.now(UTC)
            duration_ms = (completed_at - started_at).total_seconds() * 1000
            async with self._circuit_breaker_lock:
                await self._record_circuit_failure("handle")
            return ModelDispatchResult(
                dispatch_id=uuid4(),
                status=EnumDispatchStatus.HANDLER_ERROR,
                topic=TOPIC_ID_DELEGATION_WORKFLOW,
                dispatcher_id=self.dispatcher_id,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                error_message=sanitize_error_message(e),
                correlation_id=correlation_id,
                output_events=[],
            )
        except (ValidationError, ValueError, KeyError, TypeError, AttributeError) as e:
            completed_at = datetime.now(UTC)
            duration_ms = (completed_at - started_at).total_seconds() * 1000
            return ModelDispatchResult(
                dispatch_id=uuid4(),
                status=EnumDispatchStatus.INVALID_MESSAGE,
                topic=TOPIC_ID_DELEGATION_WORKFLOW,
                dispatcher_id=self.dispatcher_id,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                error_message=sanitize_error_message(e),
                correlation_id=correlation_id,
                output_events=[],
            )
        except Exception as e:
            completed_at = datetime.now(UTC)
            duration_ms = (completed_at - started_at).total_seconds() * 1000
            async with self._circuit_breaker_lock:
                await self._record_circuit_failure("handle")
            logger.error(
                "DispatcherDelegationWorkflow failed: %s",
                sanitize_error_message(e),
                extra={"correlation_id": str(correlation_id)},
            )
            return ModelDispatchResult(
                dispatch_id=uuid4(),
                status=EnumDispatchStatus.HANDLER_ERROR,
                topic=TOPIC_ID_DELEGATION_WORKFLOW,
                dispatcher_id=self.dispatcher_id,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                error_message=sanitize_error_message(e),
                correlation_id=correlation_id,
                output_events=[],
            )
