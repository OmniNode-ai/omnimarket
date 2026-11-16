# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Submit handler: validate, admit, publish and acknowledge one workflow."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_catalog import (
    WorkflowCatalog,
    validate_workflow_payload,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_wire_envelope import (
    to_canonical_wire_envelope,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.enum_workflow_refusal_code import (
    EnumWorkflowStatus,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_envelope import (
    ModelWorkflowAck,
    ModelWorkflowCommandEnvelope,
    ModelWorkflowSubmitRequest,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_handler_settings import (
    ModelWorkflowSubmitSettings,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_http import (
    ModelWorkflowHttpResponse,
    ModelWorkflowSubmitCommand,
)
from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_publisher import (
    ProtocolWorkflowPublisher,
)
from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_rate_limiter import (
    ProtocolWorkflowRateLimiter,
    WorkflowRateLimiterUnavailableError,
)
from omnimarket.nodes.node_workflow_gateway_effect.protocols.protocol_workflow_store import (
    ProtocolWorkflowStore,
    RateLimitEventRecordError,
    WorkflowStoreUnavailableError,
)

logger = logging.getLogger(__name__)


class HandlerWorkflowSubmit:
    """Submits one workflow: validate, rate-limit, admit, publish, acknowledge."""

    def __init__(
        self,
        *,
        catalog: WorkflowCatalog,
        store: ProtocolWorkflowStore,
        publisher: ProtocolWorkflowPublisher,
        rate_limiter: ProtocolWorkflowRateLimiter,
        settings: ModelWorkflowSubmitSettings,
        clock: Callable[[], datetime],
        new_uuid: Callable[[], UUID],
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._publisher = publisher
        self._rate_limiter = rate_limiter
        self._settings = settings
        self._clock = clock
        self._new_uuid = new_uuid

    async def handle(
        self, request: ModelWorkflowSubmitCommand
    ) -> ModelWorkflowHttpResponse:
        caller = request.caller

        # 1. Body validation.
        try:
            submission = ModelWorkflowSubmitRequest.model_validate(request.body)
        except ValidationError as exc:
            errors = [
                {
                    "field": ".".join(str(part) for part in err["loc"]),
                    "message": err["msg"],
                }
                for err in exc.errors()
            ]
            return ModelWorkflowHttpResponse(
                status_code=400,
                body={"detail": "invalid workflow submission", "errors": errors},
            )

        # 2. Catalog resolution.
        entry = self._catalog.resolve(submission.workflow_type)
        if entry is None:
            allowed = list(self._catalog.allowed())
            fenced_entry = self._catalog.fenced(submission.workflow_type)
            if fenced_entry is not None:
                return ModelWorkflowHttpResponse(
                    status_code=400,
                    body={
                        "detail": (
                            f"workflow_type '{submission.workflow_type}' is declared but FENCED "
                            "at this gateway: it has no runtime fit to service it. Submissions are "
                            "refused here rather than routed to a runtime that cannot service them."
                        ),
                        "fenced": True,
                        "allowed_workflow_types": allowed,
                    },
                )
            return ModelWorkflowHttpResponse(
                status_code=400,
                body={
                    "detail": f"unknown workflow_type: '{submission.workflow_type}'",
                    "allowed_workflow_types": allowed,
                },
            )

        # 3. Correlation id.
        correlation_id = submission.correlation_id or self._new_uuid()

        # 4. Rate limit.
        override: int | None = None
        try:
            override = await self._store.rpm_override(tenant_id=caller.tenant_id)
        except Exception:
            logger.warning("rpm_override lookup failed; using defaults")
            override = None

        if override is not None and override >= 1:
            refill = override
            capacity = override
        else:
            refill = self._settings.default_refill_per_minute
            capacity = self._settings.default_burst_capacity

        try:
            decision = await self._rate_limiter.acquire(
                caller.principal_id, refill_per_minute=refill, capacity=capacity
            )
        except WorkflowRateLimiterUnavailableError:
            return ModelWorkflowHttpResponse(
                status_code=503,
                body={"detail": "rate limiter temporarily unavailable"},
            )

        if not decision.allowed:
            try:
                event_id = await self._store.record_rate_limit_exceeded(
                    caller=caller,
                    workflow_type=entry.workflow_type,
                    correlation_id=correlation_id,
                    decision=decision,
                )
            except RateLimitEventRecordError:
                return ModelWorkflowHttpResponse(
                    status_code=503,
                    body={
                        "detail": "rate limit exceeded but the exceeded event could not be recorded"
                    },
                )
            return ModelWorkflowHttpResponse(
                status_code=429,
                body={
                    "detail": "workflow rate limit exceeded",
                    "workflow_type": submission.workflow_type,
                    "limit_per_minute": decision.refill_per_minute,
                    "burst_capacity": decision.capacity,
                    "retry_after_seconds": decision.retry_after_seconds,
                    "exceeded_event_id": str(event_id),
                },
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )

        # 5. Payload validation.
        errors = validate_workflow_payload(entry, submission.payload)
        if errors:
            return ModelWorkflowHttpResponse(
                status_code=400,
                body={
                    "detail": "payload does not conform to the workflow contract",
                    "workflow_type": submission.workflow_type,
                    "contract_id": entry.contract_id,
                    "errors": errors,
                },
            )

        # 6. Envelope.
        envelope_id = self._new_uuid()
        causation_id = submission.causation_id or envelope_id
        envelope = ModelWorkflowCommandEnvelope(
            envelope_id=envelope_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            source_tenant_id=caller.tenant_id,
            authenticated_user_id=caller.authenticated_user_id,
            schema_version=self._settings.envelope_schema_version,
            emitted_at=self._clock(),
            source_gateway_instance=self._settings.gateway_instance_id,
            source_tenant_principal_id=caller.principal_id,
            workflow_id=self._new_uuid(),
            workflow_type=entry.workflow_type,
            contract_id=entry.contract_id,
            command_topic=entry.command_topic,
            event_type=entry.event_type,
            payload=submission.payload,
        )

        # 7. Admit.
        try:
            admission = await self._store.admit(caller=caller, envelope=envelope)
        except WorkflowStoreUnavailableError:
            return ModelWorkflowHttpResponse(
                status_code=503,
                body={"detail": "workflow submission could not be recorded"},
            )
        if not admission.admitted:
            quota = admission.quota
            return ModelWorkflowHttpResponse(
                status_code=429,
                body={
                    "detail": "monthly workflow quota exceeded",
                    "plan_code": quota.plan_code,
                    "limit_value": quota.limit_value,
                    "used": quota.used,
                    "remaining": quota.remaining,
                    "window_start": quota.window_start,
                    "window_end": quota.window_end,
                },
            )

        # 8. Publish.
        try:
            wire = to_canonical_wire_envelope(envelope, caller.tenant_slug, entry.wire)
            await self._publisher.publish(
                topic=entry.command_topic, key=str(envelope.workflow_id), wire=wire
            )
        except Exception:
            try:
                await self._store.set_status(
                    workflow_id=envelope.workflow_id,
                    tenant_id=caller.tenant_id,
                    status=EnumWorkflowStatus.FAILED_PUBLISH,
                )
            except Exception:
                logger.warning("best-effort set_status(FAILED_PUBLISH) failed")
            return ModelWorkflowHttpResponse(
                status_code=503,
                body={
                    "detail": "workflow accepted for validation but could not be published",
                    "workflow_id": str(envelope.workflow_id),
                    "correlation_id": str(envelope.correlation_id),
                },
            )

        # 9. Best-effort PUBLISHED, then acknowledge.
        try:
            await self._store.set_status(
                workflow_id=envelope.workflow_id,
                tenant_id=caller.tenant_id,
                status=EnumWorkflowStatus.PUBLISHED,
            )
        except Exception:
            logger.warning("best-effort set_status(PUBLISHED) failed")

        ack = ModelWorkflowAck(
            workflow_id=envelope.workflow_id,
            envelope_id=envelope.envelope_id,
            correlation_id=envelope.correlation_id,
            workflow_type=envelope.workflow_type,
            status=EnumWorkflowStatus.PUBLISHED,
            accepted_at=envelope.emitted_at,
        )
        return ModelWorkflowHttpResponse(
            status_code=202, body=ack.model_dump(mode="json")
        )


__all__: list[str] = ["HandlerWorkflowSubmit"]
