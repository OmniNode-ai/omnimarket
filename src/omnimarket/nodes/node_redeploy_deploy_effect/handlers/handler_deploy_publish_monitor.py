# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deploy publish-monitor + rollback EFFECT handler (OMN-13211 / B3).

Absorbs the ``node_redeploy`` ``HandlerRedeployKafka`` (publish-monitor) and
``DeploymentAdapterKafka`` (rollback) into one canonical EFFECT node. The only
real I/O in the redeploy decomposition lives here: publish the HMAC-signed
rebuild command to the external ``.201`` deploy agent on
``onex.cmd.deploy.rebuild-requested.v1``, poll ``onex.evt.deploy.rebuild-completed.v1``
for the matching correlation_id, and — on a deploy that succeeded then failed
post-deploy health — publish the rolled-back event on
``onex.evt.omnimarket.redeploy-rolled-back.v1``.

This handler never SSHes, never calls rpk directly, has no subprocess calls. The
actual Docker rebuild and image restore are the external deploy agent's job; this
effect publishes the command, monitors the bus, and records the outcome. The
event bus is DI-injected; topics are resolved from the contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    EnumProdGrantReason,
    EnumRedeployPhase,
    EnumRedeployStatus,
    ModelDeployRebuildCommand,
    ModelDeployRebuildCompleted,
    ModelDeployRefusedEvent,
    ModelRedeployResult,
    ModelRedeployRolledBackEvent,
    verify_prod_deploy_grant_binding,
)
from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_redeploy_deploy_effect.models.model_deploy_publish_command import (
    ModelDeployPublishCommand,
)

logger = logging.getLogger(__name__)

HANDLER_ID = "redeploy-deploy-publish-monitor-effect"

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
_DEFAULT_TIMEOUT_S = 600.0
_POLL_INTERVAL_S = 2.0
_DEPLOY_AGENT_HMAC_SECRET_ENV = "DEPLOY_AGENT_HMAC_SECRET"

# Contract-declared topics (no hardcoded strings).
_SUBSCRIBE = contract_subscribe_topics(_CONTRACT)
_PUBLISH = contract_publish_topics(_CONTRACT)


def _topic_with_suffix(topics: tuple[str, ...], suffix: str, section: str) -> str:
    """Resolve exactly one contract topic ending with ``suffix``."""
    matches = [t for t in topics if t.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(
            f"Contract {_CONTRACT} must declare exactly one event_bus.{section} "
            f"topic ending in {suffix!r}; found {matches}"
        )
    return matches[0]


TOPIC_REBUILD_REQUESTED = _topic_with_suffix(
    _PUBLISH, "deploy.rebuild-requested.v1", "publish_topics"
)
TOPIC_REBUILD_COMPLETED = _topic_with_suffix(
    _SUBSCRIBE, "deploy.rebuild-completed.v1", "subscribe_topics"
)
TOPIC_ROLLED_BACK = _topic_with_suffix(
    _PUBLISH, "redeploy-rolled-back.v1", "publish_topics"
)
TOPIC_DEPLOY_REFUSED = _topic_with_suffix(
    _PUBLISH, "redeploy-deploy-refused.v1", "publish_topics"
)


def _normalize_completion_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize deploy-agent payloads to the completion model shape."""
    normalized = dict(payload)
    phase_results = dict(normalized.get("phase_results") or {})
    errors = list(normalized.get("errors") or [])

    status = normalized.get("status")
    if status is None:
        non_publish_in_progress = any(
            phase != "publish" and result == "in_progress"
            for phase, result in phase_results.items()
        )
        failed_phase = any(result == "failed" for result in phase_results.values())
        status = (
            EnumRedeployStatus.FAILED.value
            if errors or failed_phase or non_publish_in_progress
            else EnumRedeployStatus.SUCCESS.value
        )
        normalized["status"] = status

    normalized_phase_results = {}
    for phase, result in phase_results.items():
        if result == "in_progress":
            if phase == "publish":
                result = "success"
            elif status == EnumRedeployStatus.FAILED.value:
                result = "failed"
            else:
                result = "pending"
        normalized_phase_results[phase] = result
    normalized["phase_results"] = normalized_phase_results
    return normalized


def _sign_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach the deploy-agent HMAC signature when the shared secret is set."""
    secret = os.environ.get(_DEPLOY_AGENT_HMAC_SECRET_ENV, "").strip()
    if not secret:
        return payload
    body_dict = {k: v for k, v in payload.items() if k != "_signature"}
    body = json.dumps(body_dict, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {**body_dict, "_signature": signature}


def _rollback_reason(
    result: ModelRedeployResult | None, smoke_test: bool
) -> str | None:
    """Return a rollback reason for a successful deploy that fails post-checks.

    A deploy the agent reports as ``failed`` is NOT a rollback — the artifact
    never went live, so the FSM circuit breaker handles it. Rollback is only for a
    deploy that succeeded then failed post-deploy verification: a publish-monitor
    timeout, a failing ``/health`` check, or a requested smoke probe with no live
    runtime proof (fails closed, OMN-9579).
    """
    if result is None:
        return None
    if result.timed_out:
        return "deploy agent timed out before completion; rolling back"
    if not result.success:
        return None
    failing_health = [hc for hc in result.health_checks if hc.status == "fail"]
    if failing_health:
        endpoints = ", ".join(hc.endpoint for hc in failing_health)
        return f"post-deploy health check failed ({endpoints}); rolling back"
    if smoke_test:
        return "post-deploy smoke test failed (no live runtime proof); rolling back"
    return None


class HandlerDeployPublishMonitor:
    """Publish-monitor + rollback effect for the external deploy agent.

    The event bus is DI-injected (any ``ProtocolEventBus`` — ``EventBusInmemory``
    for tests, ``EventBusKafka`` in the runtime). ``handle`` publishes the rebuild
    command, polls for completion, evaluates rollback, and returns the result and
    any rolled-back event as EFFECT events.
    """

    def __init__(
        self,
        event_bus: Any,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        poll_interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        if event_bus is None:
            raise RuntimeError(  # error-ok: mis-wired constructor
                "HandlerDeployPublishMonitor requires an event_bus. Wire "
                "EventBusKafka in the runtime, or pass EventBusInmemory for tests."
            )
        self._bus: Any = event_bus
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

    @property
    def bus(self) -> Any:
        """The underlying event bus instance."""
        return self._bus

    async def handle(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        """Refuse off-gate prod deploys, else publish/monitor and roll back if needed.

        Defense-in-depth (OMN-13440): before any deploy-agent I/O, the EFFECT
        independently verifies a prod command is target-bound to a verified
        promotion grant. A prod command with no/mismatched/expired grant is REFUSED
        here — the deploy-agent rebuild command is NEVER published — and a typed
        ``ModelDeployRefusedEvent`` is emitted instead. Non-prod lanes are
        unaffected (the binding check returns ``None``), so dev/stability dispatch
        is byte-for-byte unchanged.
        """
        command = _coerce_command(envelope.payload)

        refusal = verify_prod_deploy_grant_binding(
            runtime_lane=command.runtime_lane,
            image_digest=command.image_digest,
            promotion_batch_id=command.promotion_batch_id,
            grant=command.promotion_grant,
            evaluated_at=command.evaluated_at,
        )
        if refusal is not None:
            return await self._refuse(envelope, command, refusal)

        result = await self.publish_and_monitor(command)

        emitted: list[ModelEventEnvelope[Any]] = []
        reason = _rollback_reason(result, command.smoke_test)
        if reason is not None:
            rolled_back = await self.rollback(
                correlation_id=command.correlation_id,
                runtime_lane=command.runtime_lane,
                restored_image=command.rollback_target,
                failure_reason=reason,
                failed_phase=EnumRedeployPhase.VERIFY_HEALTH,
            )
            emitted.append(
                ModelEventEnvelope(
                    payload=rolled_back,
                    correlation_id=envelope.correlation_id or command.correlation_id,
                    event_type=TOPIC_ROLLED_BACK,
                )
            )

        return ModelHandlerOutput.for_effect(
            input_envelope_id=envelope.envelope_id,
            correlation_id=envelope.correlation_id or command.correlation_id or uuid4(),
            handler_id=HANDLER_ID,
            events=tuple(emitted),
            metrics={
                "rebuild_success": 1.0 if result.success else 0.0,
                "timed_out": 1.0 if result.timed_out else 0.0,
                "rolled_back": 1.0 if reason is not None else 0.0,
            },
        )

    async def _refuse(
        self,
        envelope: ModelEventEnvelope[Any],
        command: ModelDeployPublishCommand,
        refusal: tuple[EnumProdGrantReason, str],
    ) -> ModelHandlerOutput[None]:
        """Refuse an off-gate prod deploy: emit the typed refusal, publish NOTHING else.

        The deploy-agent rebuild command is NEVER published on a refusal — the whole
        point of the EFFECT-boundary check is that a prod deploy that is not target-
        bound to a verified grant never reaches the deploy agent. The refusal is
        recorded as a durable bus fact on the contract-declared refused topic.
        """
        reason, detail = refusal
        grant = command.promotion_grant
        refused = ModelDeployRefusedEvent(
            correlation_id=command.correlation_id,
            runtime_lane=command.runtime_lane,
            requested_image_digest=command.image_digest,
            promotion_batch_id=command.promotion_batch_id,
            grant_id=grant.grant_id if grant is not None else None,
            reason=reason,
            detail=detail,
        )
        await self._bus.publish(
            TOPIC_DEPLOY_REFUSED,
            key=str(command.correlation_id).encode(),
            value=json.dumps(refused.model_dump(mode="json")).encode(),
        )
        logger.warning(
            "Prod deploy refused at the EFFECT boundary",
            extra={
                "correlation_id": str(command.correlation_id),
                "runtime_lane": command.runtime_lane.value,
                "reason": reason.value,
                "image_digest": command.image_digest,
                "promotion_batch_id": command.promotion_batch_id,
                "topic": TOPIC_DEPLOY_REFUSED,
            },
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=envelope.envelope_id,
            correlation_id=envelope.correlation_id or command.correlation_id,
            handler_id=HANDLER_ID,
            events=(
                ModelEventEnvelope(
                    payload=refused,
                    correlation_id=envelope.correlation_id or command.correlation_id,
                    event_type=TOPIC_DEPLOY_REFUSED,
                ),
            ),
            metrics={
                "rebuild_success": 0.0,
                "timed_out": 0.0,
                "rolled_back": 0.0,
                "deploy_refused": 1.0,
            },
        )

    async def publish_and_monitor(
        self, command: ModelDeployPublishCommand
    ) -> ModelRedeployResult:
        """Publish the rebuild command and wait for the matching completion event."""
        corr_id = str(command.correlation_id)

        rebuild_command = ModelDeployRebuildCommand(
            correlation_id=corr_id,
            requested_by=command.requested_by,
            scope=command.scope,
            runtime_lane=command.runtime_lane,
            build_source=command.build_source,
            services=list(command.services),
            git_ref=command.git_ref,
            image_ref=command.image_ref,
            image_digest=command.image_digest,
        )

        completion_future: asyncio.Future[ModelDeployRebuildCompleted] = (
            asyncio.get_event_loop().create_future()
        )

        async def _on_completion(message: Any) -> None:
            if completion_future.done():
                return
            try:
                raw = message.value
                if isinstance(raw, bytes | bytearray):
                    payload = json.loads(raw.decode())
                elif isinstance(raw, str):
                    payload = json.loads(raw)
                else:
                    payload = raw

                if payload.get("correlation_id", "") != corr_id:
                    return  # different rebuild, ignore
                payload = _normalize_completion_payload(payload)
                completion_future.set_result(ModelDeployRebuildCompleted(**payload))
            except Exception as exc:  # boundary-ok: bus message parse
                logger.warning(
                    "Failed to parse rebuild-completed event: %s", exc, exc_info=True
                )

        unsubscribe = await self._bus.subscribe(
            TOPIC_REBUILD_COMPLETED,
            on_message=_on_completion,
            group_id=f"redeploy-deploy-effect-{corr_id[:8]}",
        )

        command_payload = _sign_envelope(rebuild_command.model_dump(mode="json"))
        await self._bus.publish(
            TOPIC_REBUILD_REQUESTED,
            key=corr_id.encode(),
            value=json.dumps(command_payload).encode(),
        )
        logger.info(
            "Redeploy command published",
            extra={
                "correlation_id": corr_id,
                "scope": command.scope.value,
                "runtime_lane": command.runtime_lane.value,
                "build_source": command.build_source.value,
                "git_ref": command.git_ref,
                "image_digest": command.image_digest,
                "topic": TOPIC_REBUILD_REQUESTED,
            },
        )

        start_time = time.monotonic()
        timed_out = False
        completed: ModelDeployRebuildCompleted | None = None
        try:
            completed = await asyncio.wait_for(
                completion_future, timeout=self._timeout_s
            )
        except TimeoutError:
            timed_out = True
            logger.error(
                "Redeploy timed out after %ss waiting for correlation_id=%s",
                self._timeout_s,
                corr_id,
            )
        finally:
            await unsubscribe()

        elapsed = time.monotonic() - start_time

        if timed_out or completed is None:
            return ModelRedeployResult(
                correlation_id=corr_id,
                success=False,
                status=EnumRedeployStatus.FAILED,
                duration_seconds=elapsed,
                timed_out=True,
                errors=[
                    f"Timed out after {self._timeout_s}s waiting for deploy agent "
                    f"completion (correlation_id={corr_id})"
                ],
            )

        phase_results: dict[str, str] = {}
        if completed.phase_results:
            phase_results = {
                "git": completed.phase_results.git.value,
                "core": completed.phase_results.core.value,
                "runtime": completed.phase_results.runtime.value,
                "verification": completed.phase_results.verification.value,
                "publish": completed.phase_results.publish.value,
            }

        duration = (
            completed.duration_seconds if completed.duration_seconds > 0 else elapsed
        )
        success = completed.status == EnumRedeployStatus.SUCCESS

        logger.info(
            "Redeploy completed",
            extra={
                "correlation_id": corr_id,
                "status": completed.status,
                "duration_seconds": duration,
                "git_sha": completed.git_sha,
                "services_restarted": completed.services_restarted,
            },
        )

        return ModelRedeployResult(
            correlation_id=corr_id,
            success=success,
            status=completed.status,
            duration_seconds=duration,
            git_sha=completed.git_sha,
            runtime_lane=completed.runtime_lane,
            image_ref=completed.image_ref,
            image_digest=completed.image_digest,
            services_restarted=completed.services_restarted,
            phase_results=phase_results,
            errors=completed.errors,
            timed_out=False,
            health_checks=list(completed.health_checks),
        )

    async def rollback(
        self,
        correlation_id: Any,
        runtime_lane: Any,
        restored_image: str,
        failure_reason: str,
        failed_phase: EnumRedeployPhase,
    ) -> ModelRedeployRolledBackEvent:
        """Publish the rolled-back event over the bus and return it.

        The image restore itself is the deploy agent's job; this records and
        announces the rollback decision on the contract-declared topic.
        """
        event = ModelRedeployRolledBackEvent(
            correlation_id=correlation_id,
            runtime_lane=runtime_lane,
            restored_image=restored_image,
            failure_reason=failure_reason,
            failed_phase=failed_phase,
        )
        await self._bus.publish(
            TOPIC_ROLLED_BACK,
            key=str(correlation_id).encode(),
            value=json.dumps(event.model_dump(mode="json")).encode(),
        )
        return event


def _coerce_command(payload: Any) -> ModelDeployPublishCommand:
    """Coerce the dispatched payload into a ``ModelDeployPublishCommand``."""
    if isinstance(payload, ModelDeployPublishCommand):
        return payload
    if isinstance(payload, Mapping):
        return ModelDeployPublishCommand.model_validate(dict(payload))
    if hasattr(payload, "model_dump"):
        return ModelDeployPublishCommand.model_validate(payload.model_dump())
    raise TypeError(
        f"deploy publish payload must be ModelDeployPublishCommand or a mapping; "
        f"got {type(payload).__name__}"
    )


__all__: list[str] = [
    "HANDLER_ID",
    "TOPIC_DEPLOY_REFUSED",
    "TOPIC_REBUILD_COMPLETED",
    "TOPIC_REBUILD_REQUESTED",
    "TOPIC_ROLLED_BACK",
    "HandlerDeployPublishMonitor",
]
