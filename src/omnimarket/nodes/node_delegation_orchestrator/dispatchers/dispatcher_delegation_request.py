# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatcher adapter for incoming delegation request commands.

Routes ModelDelegationRequest payloads to HandlerDelegationWorkflow.handle_delegation_request(),
which creates a new FSM workflow and emits routing intents.

Related:
    - OMN-7040: Node-based delegation pipeline
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

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
from pydantic import ValidationError

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus import ProtocolEventBus

    from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
        HandlerDelegationWorkflow,
    )

__all__ = ["DispatcherDelegationRequest"]

logger = logging.getLogger(__name__)

TOPIC_ID_DELEGATION_REQUEST = "delegation.request"


class DispatcherDelegationRequest(MixinAsyncCircuitBreaker):
    """Dispatcher for incoming delegation request commands."""

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
            service_name="dispatcher.delegation.request",
            transport_type=EnumInfraTransportType.KAFKA,
        )

    @property
    def dispatcher_id(self) -> str:
        return "dispatcher.delegation.request"

    @property
    def category(self) -> EnumMessageCategory:
        return EnumMessageCategory.COMMAND

    @property
    def message_types(self) -> set[str]:
        return {"ModelDelegationRequest", "omnibase-infra.delegation-request"}

    @property
    def node_kind(self) -> EnumNodeKind:
        return EnumNodeKind.ORCHESTRATOR

    async def handle(
        self,
        envelope: ModelEventEnvelope[object] | dict[str, object],
    ) -> ModelDispatchResult:
        started_at = datetime.now(UTC)
        correlation_id, raw_payload = extract_envelope_fields(envelope)

        try:
            async with self._circuit_breaker_lock:
                await self._check_circuit_breaker("handle", correlation_id)

            payload = raw_payload
            if not isinstance(payload, ModelDelegationRequest):
                if isinstance(payload, dict):
                    payload = ModelDelegationRequest.model_validate(payload)
                else:
                    return ModelDispatchResult(
                        dispatch_id=uuid4(),
                        status=EnumDispatchStatus.INVALID_MESSAGE,
                        topic=TOPIC_ID_DELEGATION_REQUEST,
                        dispatcher_id=self.dispatcher_id,
                        started_at=started_at,
                        completed_at=started_at,
                        duration_ms=0.0,
                        error_message=f"Expected ModelDelegationRequest, got {type(payload).__name__}",
                        correlation_id=correlation_id,
                        output_events=[],
                    )

            assert isinstance(payload, ModelDelegationRequest)

            # Call the synchronous handler method
            intents = self._handler.handle_delegation_request(payload)

            completed_at = datetime.now(UTC)
            duration_ms = (completed_at - started_at).total_seconds() * 1000

            async with self._circuit_breaker_lock:
                await self._reset_circuit_breaker()

            logger.info(
                "DispatcherDelegationRequest processed request",
                extra={
                    "correlation_id": str(correlation_id),
                    "task_type": payload.task_type,
                    "intent_count": len(intents),
                    "duration_ms": duration_ms,
                },
            )

            return ModelDispatchResult(
                dispatch_id=uuid4(),
                status=EnumDispatchStatus.SUCCESS,
                topic=TOPIC_ID_DELEGATION_REQUEST,
                dispatcher_id=self.dispatcher_id,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
                output_events=list(intents),
            )

        except InfraUnavailableError as e:
            completed_at = datetime.now(UTC)
            duration_ms = (completed_at - started_at).total_seconds() * 1000
            logger.error(
                "DispatcherDelegationRequest circuit open: %s",
                sanitize_error_message(e),
                extra={"correlation_id": str(correlation_id)},
            )
            return ModelDispatchResult(
                dispatch_id=uuid4(),
                status=EnumDispatchStatus.HANDLER_ERROR,
                topic=TOPIC_ID_DELEGATION_REQUEST,
                dispatcher_id=self.dispatcher_id,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                error_message=sanitize_error_message(e),
                correlation_id=correlation_id,
                output_events=[],
            )

        except (ValidationError, ValueError, KeyError) as e:
            completed_at = datetime.now(UTC)
            duration_ms = (completed_at - started_at).total_seconds() * 1000
            logger.warning(
                "DispatcherDelegationRequest validation error: %s",
                sanitize_error_message(e),
                extra={"correlation_id": str(correlation_id)},
            )
            return ModelDispatchResult(
                dispatch_id=uuid4(),
                status=EnumDispatchStatus.INVALID_MESSAGE,
                topic=TOPIC_ID_DELEGATION_REQUEST,
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
                "DispatcherDelegationRequest failed: %s",
                sanitize_error_message(e),
                extra={"correlation_id": str(correlation_id)},
            )
            return ModelDispatchResult(
                dispatch_id=uuid4(),
                status=EnumDispatchStatus.HANDLER_ERROR,
                topic=TOPIC_ID_DELEGATION_REQUEST,
                dispatcher_id=self.dispatcher_id,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                error_message=sanitize_error_message(e),
                correlation_id=correlation_id,
                output_events=[],
            )
